"""Запуск: python -m bot.main"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand, BotCommandScopeChat

from .broadcast import Broadcaster
from .config import Config
from .db import Database
from .handlers import build_router


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    db = Database(cfg.db_path)
    await db.connect()
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    broadcaster = Broadcaster(db, cfg.broadcast_rate)
    dp = Dispatcher()
    dp.include_router(build_router(cfg, db, broadcaster))
    for admin_id in cfg.admin_ids:
        try:
            await bot.set_my_commands([
                BotCommand(command="admin", description="Админ-панель"),
                BotCommand(command="broadcast", description="Рассылка"),
                BotCommand(command="stats", description="Статистика"),
                BotCommand(command="stop", description="Остановить рассылку"),
            ], scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception:
            logging.warning("Не удалось задать меню админу %s: он ещё не писал боту", admin_id)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        broadcaster.stop()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
