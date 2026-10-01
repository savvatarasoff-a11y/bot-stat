"""Рассылка: копия сообщения админа во все активные чаты бота, счёт по факту доставки."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from .db import Database

log = logging.getLogger(__name__)

PROGRESS_EVERY = 5000
# ошибки, после которых в чат писать бессмысленно: помечаем его неактивным
GONE = ("chat not found", "user is deactivated", "bot was kicked", "group chat was deactivated",
        "have no rights to send", "not enough rights", "chat_write_forbidden")


@dataclass
class Result:
    sent: int = 0
    failed: int = 0
    gone: int = 0        # из failed: заблокировали бота / удалили аккаунт / выгнали из чата
    queued: int = 0

    @property
    def total(self) -> int:
        return self.sent + self.failed


class Broadcaster:
    def __init__(self, db: Database, rate: float = 25):
        self.db = db
        self.delay = 1 / rate if rate > 0 else 0
        self.task: asyncio.Task | None = None
        self.result: Result | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def stop(self) -> bool:
        if not self.running:
            return False
        self.task.cancel()
        return True

    async def send_one(self, bot: Bot, chat_id: int, from_chat: int, message_id: int) -> bool | None:
        """True — доставлено; False — не доставлено; None — чат недоступен навсегда."""
        for _ in range(3):
            try:
                await bot.copy_message(chat_id, from_chat, message_id)
                return True
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                return None
            except TelegramBadRequest as e:
                return None if any(s in e.message.lower() for s in GONE) else False
            except Exception:
                log.exception("Рассылка: ошибка отправки в %s", chat_id)
                return False
        return False

    async def run(self, bot: Bot, from_chat: int, message_id: int,
                  progress: Callable[[Result], Awaitable[None]] | None = None) -> Result:
        ids = await self.db.active_chat_ids()
        res = self.result = Result(queued=len(ids))
        gone: list[int] = []
        try:
            for i, chat_id in enumerate(ids, start=1):
                ok = await self.send_one(bot, chat_id, from_chat, message_id)
                if ok:
                    res.sent += 1
                else:
                    res.failed += 1
                    if ok is None:
                        res.gone += 1
                        gone.append(chat_id)
                if len(gone) >= 500:
                    await self.db.set_inactive(gone)
                    gone = []
                if progress and i % PROGRESS_EVERY == 0 and i < len(ids):
                    await progress(res)
                if self.delay:
                    await asyncio.sleep(self.delay)
        finally:
            await self.db.set_inactive(gone)
        return res

    def start(self, bot: Bot, admin_id: int, from_chat: int, message_id: int,
              progress: Callable[[Result], Awaitable[None]] | None = None,
              done: Callable[[Result, bool], Awaitable[None]] | None = None) -> None:
        async def job() -> None:
            bc_id = await self.db.broadcast_begin(admin_id)
            stopped = False
            try:
                await self.run(bot, from_chat, message_id, progress)
            except asyncio.CancelledError:
                stopped = True
            res = self.result or Result()
            await self.db.broadcast_end(bc_id, res.sent, res.failed, "stopped" if stopped else "done")
            log.info("Рассылка #%s: доставлено %s, не доставлено %s", bc_id, res.sent, res.failed)
            if done:
                await done(res, stopped)

        self.task = asyncio.create_task(job())


def report(res: Result, stopped: bool = False) -> str:
    head = "⛔️ <b>Рассылка остановлена</b>" if stopped else "✅ <b>Рассылка завершена!</b>"
    return (
        f"{head}\n\n📊 <b>Статистика:</b>\n"
        f"✅ Успешно доставлено: {res.sent} пользователям\n"
        f"❌ Не доставлено: {res.failed} пользователям\n"
        f"👥 Всего обработано: {res.total} пользователей"
        + (f"\n\n🚫 Заблокировали бота или удалены: {res.gone} (больше не получат рассылку)" if res.gone else "")
    )
