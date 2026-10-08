"""Запуск: python -m commenter (первый запуск спросит телефон и код из Telegram)."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from telethon import TelegramClient, errors, events, utils
from telethon.sessions import StringSession
from telethon.tl.functions.account import UpdateStatusRequest
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
from telethon.tl.functions.messages import GetDiscussionMessageRequest

from .config import Config
from .core import (CommentPicker, Seen, autoforward_post_id, channel_key, discussion_top,
                   thread_key)

log = logging.getLogger("commenter")


class Commenter:
    def __init__(self, client: TelegramClient, cfg: Config, picker: CommentPicker) -> None:
        self.client = client
        self.cfg = cfg
        self.picker = picker
        self.channel: Any = None        # InputPeerChannel канала
        self.group: Any = None          # InputPeerChannel чата обсуждений
        self.channel_id = 0
        self.group_id = 0
        self.last_post_id = 0
        self._requested = Seen()        # посты, по которым уже спросили GetDiscussionMessage
        self._sent = Seen()             # треды, куда уже ушёл (или уходит) комментарий

    async def setup(self) -> None:
        channel = await self.client.get_entity(self.cfg.channel)
        if self.cfg.join and getattr(channel, "left", False):
            await self.client(JoinChannelRequest(channel))
            log.info("Подписался на канал")
        full = await self.client(GetFullChannelRequest(channel))
        linked = full.full_chat.linked_chat_id
        if not linked:
            raise SystemExit("У канала выключены комментарии (нет привязанного чата обсуждений)")
        group = next(c for c in full.chats if c.id == linked)
        if self.cfg.join and getattr(group, "left", False):
            try:
                await self.client(JoinChannelRequest(group))
                log.info("Вступил в чат обсуждений")
            except errors.RPCError as e:
                log.warning("Не удалось вступить в чат обсуждений (%s) — будет только запасной путь", e)
        self.channel, self.group = utils.get_input_peer(channel), utils.get_input_peer(group)
        self.channel_id, self.group_id = channel.id, group.id
        last = await self.client.get_messages(channel, limit=1)
        self.last_post_id = last[0].id if last else 0
        log.info("Слежу за «%s», комментарии в «%s»", channel.title, group.title)

    # --- два источника id треда ---

    async def on_group_message(self, message: Any) -> None:
        """Автопересылка поста в чат обсуждений — самый быстрый путь, id уже в апдейте."""
        if autoforward_post_id(message, self.channel_id) is not None:
            await self.comment(thread_key(message), message.id)

    async def on_channel_post(self, post: Any) -> None:
        """Пост в канале — спрашиваем id треда сами, на случай если апдейт из чата задержится."""
        self.last_post_id = max(self.last_post_id, post.id)
        if getattr(post, "action", None) or not self._requested.add(channel_key(post)):
            return
        for _ in range(20):
            if not post.grouped_id and ("post", post.id) in self._sent:
                return   # чат обсуждений уже успел
            try:
                r = await self.client(GetDiscussionMessageRequest(self.channel, post.id))
            except errors.MsgIdInvalidError:
                await asyncio.sleep(0.15)   # копия в чат ещё не создана
                continue
            top = discussion_top(r.messages)
            await self.comment(thread_key(top), top.id)
            return
        log.warning("Пост %s: не нашёл тред комментариев", post.id)

    # --- отправка ---

    async def comment(self, key: Any, reply_to: int) -> None:
        if not self._sent.add(key):
            return
        started = time.perf_counter()
        if self.cfg.delay:
            await asyncio.sleep(self.cfg.delay)
        text = self.picker.next()
        try:
            await self.client.send_message(self.group, text, reply_to=reply_to, link_preview=False)
        except errors.FloodWaitError as e:
            log.error("FloodWait %s с — Telegram временно ограничил отправку", e.seconds)
        except errors.SlowModeWaitError as e:
            log.error("Медленный режим в чате, ждать %s с", e.seconds)
        except errors.RPCError as e:
            log.error("Не отправил комментарий: %s", e)
        else:
            log.info("Комментарий отправлен за %.0f мс: %s", (time.perf_counter() - started) * 1000,
                     text.replace("\n", " ")[:60])

    # --- фоновые задачи ---

    async def keep_online(self) -> None:
        """Статус «в сети» — Telegram быстрее присылает апдейты активным клиентам."""
        while True:
            try:
                await self.client(UpdateStatusRequest(offline=False))
            except errors.RPCError as e:
                log.debug("UpdateStatus: %s", e)
            await asyncio.sleep(50)

    async def poll(self) -> None:
        """Опрос канала — страховка, если апдейты от Telegram приходят с задержкой."""
        while True:
            await asyncio.sleep(self.cfg.poll_interval)
            try:
                posts = await self.client.get_messages(self.channel, limit=10, min_id=self.last_post_id)
            except errors.FloodWaitError as e:
                log.warning("Опрос: FloodWait %s с, увеличьте POLL_INTERVAL", e.seconds)
                await asyncio.sleep(e.seconds)
                continue
            for post in reversed(posts):
                asyncio.create_task(self.on_channel_post(post))


def make_session(raw: str) -> Any:
    if len(raw) > 100:   # строка StringSession
        return StringSession(raw)
    os.makedirs(os.path.dirname(raw) or ".", exist_ok=True)
    return raw


async def run(cfg: Config) -> None:
    client = TelegramClient(make_session(cfg.session), cfg.api_id, cfg.api_hash)
    await client.start()
    c = Commenter(client, cfg, CommentPicker(cfg.comments))
    await c.setup()
    client.add_event_handler(lambda e: c.on_group_message(e.message), events.NewMessage(chats=c.group))
    client.add_event_handler(lambda e: c.on_channel_post(e.message), events.NewMessage(chats=c.channel))
    tasks = [asyncio.create_task(c.keep_online())]
    if cfg.poll_interval > 0:
        tasks.append(asyncio.create_task(c.poll()))
    log.info("Жду новые посты…")
    try:
        await client.run_until_disconnected()
    finally:
        for t in tasks:
            t.cancel()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    asyncio.run(run(Config.from_env()))


if __name__ == "__main__":
    main()
