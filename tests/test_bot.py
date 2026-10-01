"""Бот через диспетчер aiogram с поддельным Telegram."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from pydantic import TypeAdapter
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import CopyMessage, SendMessage, TelegramMethod
from aiogram.types import Update

from bot.broadcast import Broadcaster
from bot.config import Config
from bot.db import Database
from bot.handlers import build_router

ADMIN = 5349009098


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self.blocked: set[int] = set()
        self.flood_once: set[int] = set()

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None) -> Any:
        self.calls.append(method)
        if isinstance(method, CopyMessage):
            if method.chat_id in self.blocked:
                raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user")
            if method.chat_id in self.flood_once:
                self.flood_once.discard(method.chat_id)
                raise TelegramRetryAfter(method=method, message="Too Many Requests", retry_after=0)
            raw: Any = {"message_id": 5}
        elif isinstance(method, SendMessage):
            raw = {"message_id": 1, "date": int(time.time()), "chat": {"id": method.chat_id, "type": "private"},
                   "text": method.text}
        else:
            raw = True
        return TypeAdapter(method.__returning__).validate_python(raw, context={"bot": bot})

    async def close(self) -> None:
        pass

    async def stream_content(self, *a: Any, **kw: Any):  # pragma: no cover
        yield b""

    def texts(self, chat_id: int) -> list[str]:
        return [c.text for c in self.calls if isinstance(c, SendMessage) and c.chat_id == chat_id]

    def copies(self) -> list[int]:
        return [c.chat_id for c in self.calls if isinstance(c, CopyMessage)]


def msg(uid: int, chat_id: int, text: str, chat_type: str = "private", from_id: int | None = None) -> dict:
    chat = {"id": chat_id, "type": chat_type}
    chat.update({"first_name": f"U{chat_id}"} if chat_type == "private" else {"title": f"G{chat_id}"})
    m = {"message_id": uid, "date": int(time.time()), "chat": chat,
         "from": {"id": from_id or chat_id, "is_bot": False, "first_name": "U"}, "text": text}
    if text.startswith("/"):
        m["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return {"update_id": uid, "message": m}


def member(uid: int, chat_id: int, chat_type: str, status: str) -> dict:
    bot_user = {"id": 1, "is_bot": True, "first_name": "Bot"}
    chat = {"id": chat_id, "type": chat_type, "title": "Group"}
    return {"update_id": uid, "my_chat_member": {
        "chat": chat, "from": {"id": 7, "is_bot": False, "first_name": "A"}, "date": int(time.time()),
        "old_chat_member": {"status": "left", "user": bot_user},
        "new_chat_member": ({"status": "kicked", "user": bot_user, "until_date": 0} if status == "kicked"
                            else {"status": status, "user": bot_user})}}


@pytest.fixture
async def env(tmp_path):
    cfg = Config(bot_token="1:x", admin_ids=frozenset({ADMIN}), db_path=str(tmp_path / "b.db"), broadcast_rate=0)
    db = Database(cfg.db_path)
    await db.connect()
    session = FakeSession()
    bot = Bot("1:x", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    broadcaster = Broadcaster(db, cfg.broadcast_rate)
    dp = Dispatcher()
    dp.include_router(build_router(cfg, db, broadcaster))

    async def feed(raw: dict) -> None:
        await dp.feed_update(bot, Update.model_validate(raw, context={"bot": bot}))

    yield {"feed": feed, "session": session, "db": db, "broadcaster": broadcaster}
    broadcaster.stop()
    await db.close()


async def wait_done(broadcaster: Broadcaster) -> None:
    for _ in range(200):
        if broadcaster.task and broadcaster.task.done():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("рассылка не завершилась")


async def test_broadcast_counts_real_deliveries(env):
    feed, session, db, bc = env["feed"], env["session"], env["db"], env["broadcaster"]
    for i, uid in enumerate((301, 302, 303), start=1):
        await feed(msg(i, uid, f"/start ref{uid % 2}"))
    await feed(member(10, -100500, "supergroup", "member"))     # бота добавили в группу
    await feed(member(11, -100600, "channel", "member"))
    await feed(member(12, -100600, "channel", "left"))           # ...и убрали из канала
    session.blocked = {302}
    session.flood_once = {303}                                   # флуд-лимит: подождать и повторить

    await feed(msg(20, ADMIN, "/admin"))
    assert "Админ-панель" in session.texts(ADMIN)[-1]
    await feed(msg(21, ADMIN, "/broadcast"))
    assert session.texts(ADMIN)[-1] == "✉️ Введите текст для рассылки:"
    await feed(msg(22, ADMIN, "/help"))
    assert "Это команда" in session.texts(ADMIN)[-1]
    await feed(msg(23, ADMIN, "🎁 Чек на 10 ⭐"))
    assert "Начинаю текстовую рассылку" in session.texts(ADMIN)[-1]
    await wait_done(bc)

    report = session.texts(ADMIN)[-1]
    # получатели: 301, 302, 303, группа и сам админ; канал, откуда бота убрали, пропущен
    assert "Успешно доставлено: 4 пользователям" in report
    assert "Не доставлено: 1 пользователям" in report
    assert "Всего обработано: 5 пользователей" in report
    assert -100600 not in session.copies() and session.copies().count(303) == 2

    # заблокировавший больше не в рассылке, пока снова не напишет
    assert (await db.one("SELECT active FROM chats WHERE id=302"))["active"] == 0
    await feed(msg(30, ADMIN, "/stats"))
    stats = session.texts(ADMIN)[-1]
    assert "Пользователей: 4 (доступны для рассылки: 3)" in stats
    assert "Групп и каналов: 2 (активны: 1)" in stats
    assert "<code>ref1</code> — 2" in stats and "доставлено 4, не доставлено 1 (готово)" in stats
    await feed(msg(31, 302, "/start"))
    assert (await db.one("SELECT active FROM chats WHERE id=302"))["active"] == 1


async def test_non_admin_has_no_panel(env):
    feed, session, db = env["feed"], env["session"], env["db"]
    for i, text in enumerate(("/admin", "/broadcast", "/stats", "/stop"), start=1):
        await feed(msg(i, 777, text))
    assert not any("Админ" in t or "рассылк" in t or "Статистика" in t for t in session.texts(777))
    assert session.copies() == []
    assert (await db.one("SELECT COUNT(*) n FROM chats"))["n"] == 1    # но сам пользователь учтён


async def test_cancel_and_stop(env):
    feed, session, bc = env["feed"], env["session"], env["broadcaster"]
    await feed(msg(1, ADMIN, "/broadcast"))
    await feed(msg(2, ADMIN, "/cancel"))
    await feed(msg(3, ADMIN, "просто сообщение"))
    assert session.copies() == [] and bc.task is None

    await feed(msg(4, ADMIN, "/stop"))
    assert "не идёт" in session.texts(ADMIN)[-1]
    bc.delay = 0.05
    for uid in range(1000, 1050):
        await env["db"].upsert_chat(uid, "private", None, None)
    await feed(msg(5, ADMIN, "/broadcast"))
    await feed(msg(6, ADMIN, "текст"))
    await asyncio.sleep(0.12)
    await feed(msg(7, ADMIN, "/stop"))
    await wait_done(bc)
    assert "Рассылка остановлена" in session.texts(ADMIN)[-1]
    assert len(session.copies()) < 51
