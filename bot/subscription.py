"""Обязательная подписка: скачивать видео можно только подписчикам каналов из REQUIRED_CHANNELS."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger(__name__)

SUBSCRIBED = ("creator", "administrator", "member")


async def missing_channels(bot: Bot, channels: tuple[str, ...], user_id: int) -> list[str]:
    """Каналы, на которые пользователь не подписан.

    Если Telegram не даёт проверить (бот не админ в канале, канал не найден), канал не блокирует
    пользователя: лучше пропустить, чем сломать бота для всех."""
    missing = []
    for channel in channels:
        try:
            m = await bot.get_chat_member(channel, user_id)
        except Exception as e:
            log.warning("ОП: не удалось проверить подписку на %s (бот должен быть админом канала): %s", channel, e)
            continue
        if m.status not in SUBSCRIBED and not (m.status == "restricted" and getattr(m, "is_member", False)):
            missing.append(channel)
    return missing


def channel_url(channel: str) -> str:
    return f"https://t.me/{channel.lstrip('@')}"


def subscribe_text(channels: list[str]) -> str:
    names = ", ".join(channels)
    return (f"📢 Чтобы скачивать видео, подпишитесь на {'канал' if len(channels) == 1 else 'каналы'} {names}\n\n"
            "Потом нажмите «✅ Я подписался».")


def subscribe_keyboard(channels: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"📢 Подписаться на {c}", url=channel_url(c))] for c in channels]
    rows.append([InlineKeyboardButton(text="✅ Я подписался", callback_data="sub:check")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
