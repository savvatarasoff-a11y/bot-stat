"""Команды: /start для всех, админ-панель с рассылкой и статистикой."""
from __future__ import annotations

import html

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup,
                           Message)

from .broadcast import Broadcaster, Result, report
from .config import Config
from .db import Database
from .stats import (HISTORY_LIMITS, METRICS, PERIODS, SECTIONS, SOURCES_LIMITS, TITLE_MAX, StatsSettings,
                    describe_rule, load_overrides, load_settings, next_value, numbers_keyboard, numbers_text,
                    parse_rule, save_overrides, save_settings, settings_keyboard, settings_text, stats_keyboard,
                    stats_text)


class BroadcastForm(StatesGroup):
    message = State()


class StatsForm(StatesGroup):
    title = State()
    number = State()


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Рассылка", callback_data="adm:broadcast")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats")],
        [InlineKeyboardButton(text="⚙️ Настройка статистики", callback_data="st:cfg")],
    ])


def chat_title(chat) -> str | None:
    if chat.type == "private":
        return " ".join(x for x in (chat.first_name, chat.last_name) if x) or None
    return chat.title


async def edit(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    """Перерисовать сообщение с кнопками; «не изменилось» — не ошибка."""
    try:
        await query.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in e.message:
            raise


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
            "/stats_settings — что показывать в статистике\n"
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
        await message.answer(await stats_text(db, broadcaster), reply_markup=stats_keyboard())

    @admin.callback_query(F.data == "adm:stats")
    async def stats_btn(query: CallbackQuery) -> None:
        await query.answer()
        await query.message.answer(await stats_text(db, broadcaster), reply_markup=stats_keyboard())

    @admin.callback_query(F.data == "st:show")
    async def stats_refresh(query: CallbackQuery) -> None:
        await query.answer()
        await edit(query, await stats_text(db, broadcaster), stats_keyboard())

    # ---------- настройка статистики ----------

    @admin.message(Command("stats_settings"))
    async def stats_settings_cmd(message: Message, state: FSMContext) -> None:
        await state.clear()
        s = await load_settings(db)
        await message.answer(settings_text(s), reply_markup=settings_keyboard(s))

    @admin.callback_query(F.data == "st:cfg")
    async def stats_settings_btn(query: CallbackQuery) -> None:
        await query.answer()
        s = await load_settings(db)
        await edit(query, settings_text(s), settings_keyboard(s))

    @admin.callback_query(F.data.startswith("st:sec:") | F.data.startswith("st:per:")
                          | F.data.in_({"st:src", "st:hist", "st:reset"}))
    async def stats_settings_change(query: CallbackQuery) -> None:
        s = await load_settings(db)
        data = query.data
        if data.startswith("st:sec:") and data[7:] in SECTIONS:
            s.toggle_section(data[7:])
        elif data.startswith("st:per:") and data[7:].isdigit() and int(data[7:]) in PERIODS:
            s.toggle_period(int(data[7:]))
        elif data == "st:src":
            s.sources_limit = next_value(SOURCES_LIMITS, s.sources_limit)
        elif data == "st:hist":
            s.history_limit = next_value(HISTORY_LIMITS, s.history_limit)
        elif data == "st:reset":
            s = StatsSettings()
        await save_settings(db, s)
        await query.answer("Сохранено")
        await edit(query, settings_text(s), settings_keyboard(s))

    @admin.callback_query(F.data == "st:nums")
    async def numbers_menu(query: CallbackQuery, state: FSMContext) -> None:
        await query.answer()
        await state.clear()
        await edit(query, await numbers_text(db), await numbers_keyboard(db))

    @admin.callback_query(F.data.startswith("st:num:"))
    async def number_ask(query: CallbackQuery, state: FSMContext) -> None:
        key = query.data[7:]
        if key not in METRICS:
            await query.answer()
            return
        await query.answer()
        await state.set_state(StatsForm.number)
        await state.update_data(metric=key)
        rule = (await load_overrides(db)).get(key)
        now = f"\nСейчас: {describe_rule(rule)}." if rule else ""
        buttons = [[InlineKeyboardButton(text="♻️ Вернуть реальное", callback_data=f"st:numdel:{key}")]] if rule else []
        await query.message.answer(
            f"✏️ <b>{METRICS[key]}</b>{now}\n\n"
            "Пришлите <code>1500</code> (ровно столько), <code>+500</code> или <code>-20</code> "
            "(поправка к реальному значению), либо /cancel",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None)

    @admin.callback_query(F.data.startswith("st:numdel:") | (F.data == "st:numclr"))
    async def number_reset(query: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        overrides = await load_overrides(db)
        if query.data == "st:numclr":
            overrides = {}
        else:
            overrides.pop(query.data[10:], None)
        await save_overrides(db, overrides)
        await query.answer("Реальные цифры возвращены")
        if query.data == "st:numclr":
            await edit(query, await numbers_text(db), await numbers_keyboard(db))
        else:
            await query.message.answer(await numbers_text(db), reply_markup=await numbers_keyboard(db))

    @admin.callback_query(F.data == "st:title")
    async def stats_title_ask(query: CallbackQuery, state: FSMContext) -> None:
        await query.answer()
        await state.set_state(StatsForm.title)
        await query.message.answer(f"✏️ Пришлите новый заголовок статистики (до {TITLE_MAX} символов) или /cancel")

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

    @admin.message(StateFilter(StatsForm.title), F.chat.type == "private")
    async def stats_title_set(message: Message, state: FSMContext) -> None:
        title = (message.text or "").strip()
        if not title or title.startswith("/"):
            await message.answer("Пришлите заголовок текстом или /cancel")
            return
        await state.clear()
        s = await load_settings(db)
        s.title = title[:TITLE_MAX]
        await save_settings(db, s)
        await message.answer(settings_text(s), reply_markup=settings_keyboard(s))

    @admin.message(StateFilter(StatsForm.number), F.chat.type == "private")
    async def number_set(message: Message, state: FSMContext) -> None:
        rule = parse_rule(message.text or "")
        if rule is None:
            await message.answer("Нужно число: <code>1500</code>, <code>+500</code> или <code>-20</code>. "
                                 "Или /cancel")
            return
        key = (await state.get_data()).get("metric")
        await state.clear()
        if key not in METRICS:
            return
        overrides = await load_overrides(db)
        if rule in ("+0", "-0"):
            overrides.pop(key, None)       # нулевая поправка = реальное значение
        else:
            overrides[key] = rule
        await save_overrides(db, overrides)
        await message.answer(await numbers_text(db), reply_markup=await numbers_keyboard(db))

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
