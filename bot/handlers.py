"""Команды: /start для всех, админ-панель с рассылкой и статистикой."""
from __future__ import annotations

import html
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup,
                           Message)

from .broadcast import Broadcaster, Result, report
from .config import Config
from .db import Database


class BroadcastForm(StatesGroup):
    message = State()


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Рассылка", callback_data="adm:broadcast")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats")],
    ])


def chat_title(chat) -> str | None:
    if chat.type == "private":
        return " ".join(x for x in (chat.first_name, chat.last_name) if x) or None
    return chat.title


async def stats_text(db: Database, broadcaster: Broadcaster) -> str:
    s = await db.stats()
    lines = [
        "📊 <b>Статистика бота</b>\n",
        f"👤 Пользователей: {s['users']} (доступны для рассылки: {s['users_active']})",
        f"👥 Групп и каналов: {s['groups']} (активны: {s['groups_active']})",
        f"📬 Всего получат рассылку: {s['active']}",
        f"🆕 Новых за сутки: {s['day']}, за неделю: {s['week']}",
        f"🔥 Активны за сутки: {s['seen_day']}",
    ]
    sources = await db.top_sources(5)
    if sources:
        lines.append("\n🔗 <b>Источники</b> (параметр /start):")
        lines += [f"<code>{html.escape(r['start_arg'])}</code> — {r['n']}" for r in sources]
    history = await db.last_broadcasts(3)
    if history:
        lines.append("\n📨 <b>Последние рассылки:</b>")
        for b in history:
            when = time.strftime("%d.%m %H:%M", time.localtime(b["started_at"]))
            status = {"running": "идёт", "done": "готово", "stopped": "остановлена"}[b["status"]]
            lines.append(f"{when} — доставлено {b['sent']}, не доставлено {b['failed']} ({status})")
    if broadcaster.running and broadcaster.result:
        r = broadcaster.result
        lines.append(f"\n⏳ Сейчас идёт рассылка: {r.total} из {r.queued}")
    return "\n".join(lines)


def build_router(cfg: Config, db: Database, broadcaster: Broadcaster) -> Router:
    root = Router(name="root")
    admin = Router(name="admin")
    admin.message.filter(F.from_user.id.in_(cfg.admin_ids))
    admin.callback_query.filter(F.from_user.id.in_(cfg.admin_ids))
    users = Router(name="users")

    # ---------- админ ----------

    async def ask_broadcast(message: Message, state: FSMContext) -> None:
        if broadcaster.running:
            r = broadcaster.result
            await message.answer(f"⏳ Рассылка уже идёт: {r.total if r else 0} из {r.queued if r else 0}.\n"
                                 "Остановить: /stop")
            return
        await state.set_state(BroadcastForm.message)
        await message.answer("✉️ Введите текст для рассылки:")

    @admin.message(Command("admin"))
    async def admin_panel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "🛠 <b>Админ-панель</b>\n\n"
            "/broadcast — рассылка по всем чатам бота\n"
            "/stop — остановить текущую рассылку\n"
            "/stats — статистика\n"
            "/cancel — отменить ввод",
            reply_markup=admin_keyboard())

    @admin.message(Command("broadcast"))
    async def broadcast_cmd(message: Message, state: FSMContext) -> None:
        await ask_broadcast(message, state)

    @admin.callback_query(F.data == "adm:broadcast")
    async def broadcast_btn(query: CallbackQuery, state: FSMContext) -> None:
        await query.answer()
        await ask_broadcast(query.message, state)

    @admin.message(Command("stats"))
    async def stats_cmd(message: Message) -> None:
        await message.answer(await stats_text(db, broadcaster))

    @admin.callback_query(F.data == "adm:stats")
    async def stats_btn(query: CallbackQuery) -> None:
        await query.answer()
        await query.message.answer(await stats_text(db, broadcaster))

    @admin.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Отменено.")

    @admin.message(Command("stop"))
    async def stop(message: Message) -> None:
        if not broadcaster.stop():
            await message.answer("Сейчас рассылка не идёт.")

    @admin.message(StateFilter(BroadcastForm.message), F.chat.type == "private")
    async def broadcast_go(message: Message, state: FSMContext, bot: Bot) -> None:
        if (message.text or "").startswith("/"):
            await message.answer("Это команда. Пришлите сообщение для рассылки или /cancel")
            return
        await state.clear()
        if broadcaster.running:
            await message.answer("⏳ Рассылка уже идёт. Остановить: /stop")
            return
        chat_id = message.chat.id
        kind = "текстовую " if message.text else ""
        await message.answer(f"⏳ Начинаю {kind}рассылку... Это может занять некоторое время.")

        async def progress(res: Result) -> None:
            await bot.send_message(chat_id, f"📨 Обработано {res.total} из {res.queued}, доставлено {res.sent}")

        async def done(res: Result, stopped: bool) -> None:
            await bot.send_message(chat_id, report(res, stopped))

        broadcaster.start(bot, message.from_user.id, chat_id, message.message_id, progress, done)

    # ---------- все ----------

    @root.message.outer_middleware()
    async def remember_chat(handler, message: Message, data: dict):
        """Каждый чат, где боту пишут, попадает в базу (и снова активен, если был заблокирован)."""
        chat, text = message.chat, message.text or ""
        start_arg = text.split(maxsplit=1)[1][:64] if text.startswith("/start ") and " " in text.strip() else None
        await db.upsert_chat(chat.id, chat.type, chat_title(chat), chat.username, start_arg)
        return await handler(message, data)

    @users.message(CommandStart(), F.chat.type == "private")
    async def start(message: Message) -> None:
        await message.answer(f"👋 Привет, {html.escape(message.from_user.first_name)}!")

    @users.my_chat_member()
    async def membership(event: ChatMemberUpdated) -> None:
        """Бота добавили/выгнали из группы или канала, пользователь заблокировал/разблокировал бота."""
        chat = event.chat
        if event.new_chat_member.status in ("kicked", "left"):
            await db.set_inactive([chat.id])
        else:
            await db.upsert_chat(chat.id, chat.type, chat_title(chat), chat.username)

    root.include_router(admin)
    root.include_router(users)
    return root
