"""Первый комментарий под новыми постами канала — один файл для Pydroid 3.

1. В Pydroid 3: Меню → Pip → установить  telethon
2. Заполните НАСТРОЙКИ ниже (API_ID и API_HASH — с https://my.telegram.org → API development tools).
3. Нажмите ▶. Первый раз спросит телефон (+7...), код из Telegram и пароль 2FA, если есть.
   Потом вход сохраняется в файл сессии рядом со скриптом.
"""

# ======================= НАСТРОЙКИ =======================
API_ID = 0                    # число с my.telegram.org
API_HASH = ""                 # строка с my.telegram.org
CHANNEL = "@channel"          # канал: @username, t.me/username или -100...
COMMENTS = [                  # тексты; берутся по кругу в случайном порядке
    "Первый!",
]
COMMENT_DELAY = 0             # пауза перед отправкой, сек (0 — сразу)
POLL_INTERVAL = 0             # доп. опрос канала, сек (например 1); 0 — выкл
AUTO_JOIN = True              # вступить в канал и чат обсуждений, если ещё не там
SESSION = "commenter"         # имя файла сессии
# =========================================================

import asyncio
import logging
import os
import random
import time
from collections import OrderedDict

from telethon import TelegramClient, errors, events, utils
from telethon.tl.functions.account import UpdateStatusRequest
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
from telethon.tl.functions.messages import GetDiscussionMessageRequest

log = logging.getLogger("commenter")


class Seen:
    """Множество последних ключей с ограниченным размером."""

    def __init__(self, limit=2000):
        self._keys = OrderedDict()
        self._limit = limit

    def __contains__(self, key):
        return key in self._keys

    def add(self, key):
        if key in self._keys:
            return False
        self._keys[key] = None
        if len(self._keys) > self._limit:
            self._keys.popitem(last=False)
        return True


class CommentPicker:
    """Тексты по кругу в случайном порядке, без повтора подряд."""

    def __init__(self, comments):
        self._comments = [c for c in comments if c.strip()]
        if not self._comments:
            raise SystemExit("Заполните COMMENTS")
        self._queue = []
        self._last = None
        self._refill()

    def _refill(self):
        self._queue = self._comments[:]
        random.shuffle(self._queue)
        if len(self._queue) > 1 and self._queue[-1] == self._last:
            self._queue.reverse()

    def next(self):
        self._last = self._queue.pop()
        if not self._queue:
            self._refill()
        return self._last


def parse_channel(raw):
    raw = str(raw).strip().replace("https://", "").replace("http://", "").replace("t.me/", "")
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw if raw.startswith("@") else "@" + raw


def autoforward_post_id(message, channel_id):
    """id поста, если message — автопересылка поста канала в чат обсуждений."""
    fwd = getattr(message, "fwd_from", None)
    if fwd is None or not fwd.channel_post:
        return None
    for peer in (fwd.saved_from_peer, fwd.from_id):
        if getattr(peer, "channel_id", None) == channel_id:
            return fwd.saved_from_msg_id or fwd.channel_post
    return None


def thread_key(m):
    if m.grouped_id:
        return ("album", m.grouped_id)
    fwd = m.fwd_from
    return ("post", (fwd.saved_from_msg_id or fwd.channel_post) if fwd else m.id)


class Commenter:
    """Два пути к треду комментариев, кто первый узнал id — тот и отправляет:
    1) автопересылка поста в чат обсуждений (id в апдейте, 1 запрос);
    2) пост в канале + GetDiscussionMessage (2 запроса, страховка)."""

    def __init__(self, client):
        self.client = client
        self.picker = CommentPicker(COMMENTS)
        self.channel = self.group = None
        self.channel_id = 0
        self.last_post_id = 0
        self._requested = Seen()
        self._sent = Seen()

    async def setup(self):
        channel = await self.client.get_entity(parse_channel(CHANNEL))
        if AUTO_JOIN and getattr(channel, "left", False):
            await self.client(JoinChannelRequest(channel))
            log.info("Подписался на канал")
        full = await self.client(GetFullChannelRequest(channel))
        linked = full.full_chat.linked_chat_id
        if not linked:
            raise SystemExit("У канала выключены комментарии")
        group = next(c for c in full.chats if c.id == linked)
        if AUTO_JOIN and getattr(group, "left", False):
            try:
                await self.client(JoinChannelRequest(group))
                log.info("Вступил в чат обсуждений")
            except errors.RPCError as e:
                log.warning("Не вступил в чат обсуждений (%s) — работает только запасной путь", e)
        self.channel, self.group = utils.get_input_peer(channel), utils.get_input_peer(group)
        self.channel_id = channel.id
        last = await self.client.get_messages(channel, limit=1)
        self.last_post_id = last[0].id if last else 0
        log.info("Слежу за «%s», комментарии в «%s»", channel.title, group.title)

    async def on_group_message(self, message):
        if autoforward_post_id(message, self.channel_id) is not None:
            await self.comment(thread_key(message), message.id)

    async def on_channel_post(self, post):
        self.last_post_id = max(self.last_post_id, post.id)
        key = ("album", post.grouped_id) if post.grouped_id else ("post", post.id)
        if getattr(post, "action", None) or not self._requested.add(key):
            return
        for _ in range(20):
            if not post.grouped_id and ("post", post.id) in self._sent:
                return
            try:
                r = await self.client(GetDiscussionMessageRequest(self.channel, post.id))
            except errors.MsgIdInvalidError:
                await asyncio.sleep(0.15)
                continue
            top = min(r.messages, key=lambda m: m.id)
            await self.comment(thread_key(top), top.id)
            return
        log.warning("Пост %s: не нашёл тред комментариев", post.id)

    async def comment(self, key, reply_to):
        if not self._sent.add(key):
            return
        started = time.perf_counter()
        if COMMENT_DELAY:
            await asyncio.sleep(COMMENT_DELAY)
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
            log.info("Комментарий отправлен за %.0f мс: %s",
                     (time.perf_counter() - started) * 1000, text.replace("\n", " ")[:60])

    async def keep_online(self):
        while True:
            try:
                await self.client(UpdateStatusRequest(offline=False))
            except errors.RPCError:
                pass
            await asyncio.sleep(50)

    async def poll(self):
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                posts = await self.client.get_messages(self.channel, limit=10, min_id=self.last_post_id)
            except errors.FloodWaitError as e:
                log.warning("Опрос: FloodWait %s с, увеличьте POLL_INTERVAL", e.seconds)
                await asyncio.sleep(e.seconds)
                continue
            for post in reversed(posts):
                asyncio.ensure_future(self.on_channel_post(post))


async def run():
    if not API_ID or not API_HASH:
        raise SystemExit("Заполните API_ID и API_HASH в начале файла (с https://my.telegram.org)")
    session = os.path.join(os.path.dirname(os.path.abspath(__file__)), SESSION)
    client = TelegramClient(session, API_ID, API_HASH)
    await client.start()
    c = Commenter(client)
    await c.setup()
    client.add_event_handler(lambda e: c.on_group_message(e.message), events.NewMessage(chats=c.group))
    client.add_event_handler(lambda e: c.on_channel_post(e.message), events.NewMessage(chats=c.channel))
    tasks = [asyncio.ensure_future(c.keep_online())]
    if POLL_INTERVAL > 0:
        tasks.append(asyncio.ensure_future(c.poll()))
    log.info("Жду новые посты… (остановить — кнопка ■)")
    try:
        await client.run_until_disconnected()
    finally:
        for t in tasks:
            t.cancel()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    asyncio.run(run())
