"""Комментатор: гонка двух путей с поддельным клиентом Telethon."""
from __future__ import annotations

import asyncio
import random
from datetime import datetime

from telethon import errors
from telethon.tl.functions.messages import GetDiscussionMessageRequest
from telethon.tl.types import (InputPeerChannel, Message, MessageFwdHeader, PeerChannel)

from commenter.config import Config, parse_channel, parse_comments
from commenter.core import CommentPicker, autoforward_post_id
from commenter.main import Commenter

CHANNEL, GROUP = 111, 222
NOW = datetime(2026, 1, 1)


def post(msg_id: int, grouped_id: int | None = None) -> Message:
    return Message(id=msg_id, peer_id=PeerChannel(CHANNEL), date=NOW, message="пост", grouped_id=grouped_id)


def autoforward(msg_id: int, post_id: int, grouped_id: int | None = None) -> Message:
    fwd = MessageFwdHeader(date=NOW, from_id=PeerChannel(CHANNEL), channel_post=post_id,
                           saved_from_peer=PeerChannel(CHANNEL), saved_from_msg_id=post_id)
    return Message(id=msg_id, peer_id=PeerChannel(GROUP), date=NOW, message="пост", fwd_from=fwd,
                   grouped_id=grouped_id)


class Result:
    def __init__(self, messages: list[Message]) -> None:
        self.messages = messages


class FakeClient:
    def __init__(self, discussion: dict[int, list[Message]], not_ready: int = 0, rpc_delay: float = 0) -> None:
        self.discussion = discussion
        self.not_ready = not_ready      # сколько раз ответить MSG_ID_INVALID
        self.rpc_delay = rpc_delay
        self.sent: list[tuple[str, int]] = []
        self.requests = 0

    async def __call__(self, request):
        assert isinstance(request, GetDiscussionMessageRequest)
        self.requests += 1
        await asyncio.sleep(self.rpc_delay)
        if self.not_ready:
            self.not_ready -= 1
            raise errors.MsgIdInvalidError(request)
        return Result(self.discussion[request.msg_id])

    async def send_message(self, entity, text, reply_to, link_preview):
        self.sent.append((text, reply_to))


def make(client: FakeClient, comments=("Первый!",)) -> Commenter:
    cfg = Config(api_id=1, api_hash="x", channel="@c", comments=comments)
    c = Commenter(client, cfg, CommentPicker(comments))
    c.channel, c.group = InputPeerChannel(CHANNEL, 0), InputPeerChannel(GROUP, 0)
    c.channel_id, c.group_id = CHANNEL, GROUP
    return c


def test_parse() -> None:
    assert parse_comments("Первый! | Топ\\nчик |") == ("Первый!", "Топ\nчик")
    assert parse_channel("https://t.me/durov") == "@durov"
    assert parse_channel("durov") == "@durov"
    assert parse_channel("-1001234") == -1001234


def test_autoforward_detection() -> None:
    assert autoforward_post_id(autoforward(50, 7), CHANNEL) == 7
    assert autoforward_post_id(autoforward(50, 7), 999) is None
    assert autoforward_post_id(Message(id=1, peer_id=PeerChannel(GROUP), date=NOW, message="hi"), CHANNEL) is None


def test_picker_cycles_without_repeats() -> None:
    p = CommentPicker(("a", "b", "c"), random.Random(1))
    seq = [p.next() for _ in range(30)]
    assert all(x != y for x, y in zip(seq, seq[1:]))
    assert sorted(seq[:3]) == ["a", "b", "c"]


async def test_group_path_wins_and_comments_once() -> None:
    client = FakeClient({7: [autoforward(50, 7)]}, rpc_delay=0.05)
    c = make(client)
    await asyncio.gather(c.on_channel_post(post(7)), c.on_group_message(autoforward(50, 7)))
    assert client.sent == [("Первый!", 50)]


async def test_channel_path_when_group_update_late() -> None:
    client = FakeClient({7: [autoforward(50, 7)]}, not_ready=2)
    c = make(client)
    await c.on_channel_post(post(7))
    await c.on_group_message(autoforward(50, 7))   # опоздавший апдейт — дубля нет
    await c.on_channel_post(post(7))                # тот же пост из опроса — тоже нет
    assert client.sent == [("Первый!", 50)]
    assert client.requests == 3


async def test_album_comments_once_to_top_message() -> None:
    album = [autoforward(51, 8, grouped_id=900), autoforward(50, 7, grouped_id=900)]
    client = FakeClient({7: album, 8: album})
    c = make(client)
    await asyncio.gather(c.on_channel_post(post(7, grouped_id=5)), c.on_channel_post(post(8, grouped_id=5)),
                         c.on_group_message(album[1]), c.on_group_message(album[0]))
    assert client.sent == [("Первый!", 50)]


async def test_ignores_regular_group_messages() -> None:
    client = FakeClient({})
    c = make(client)
    await c.on_group_message(Message(id=1, peer_id=PeerChannel(GROUP), date=NOW, message="привет"))
    assert client.sent == []
