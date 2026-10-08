"""Логика гонки за первый комментарий, без сети — чтобы проверять тестами.

Под постом канала комментарий — это ответ на копию поста, которую Telegram сам
пересылает в привязанный чат обсуждений. Узнать id этой копии можно двумя путями:
  1) дождаться автопересылки в чате обсуждений (апдейт сразу содержит id) — 1 запрос на отправку;
  2) получив пост в канале, спросить GetDiscussionMessage — 2 запроса.
Запускаем оба, кто первым узнал id — тот и отправляет; второй молча выходит.
"""
from __future__ import annotations

import random
from collections import OrderedDict
from typing import Any, Hashable


class Seen:
    """Множество последних ключей с ограниченным размером."""

    def __init__(self, limit: int = 2000) -> None:
        self._keys: OrderedDict[Hashable, None] = OrderedDict()
        self._limit = limit

    def __contains__(self, key: Hashable) -> bool:
        return key in self._keys

    def add(self, key: Hashable) -> bool:
        """True, если ключ новый (и теперь запомнен)."""
        if key in self._keys:
            return False
        self._keys[key] = None
        if len(self._keys) > self._limit:
            self._keys.popitem(last=False)
        return True


def peer_channel_id(peer: Any) -> int | None:
    return getattr(peer, "channel_id", None)


def autoforward_post_id(message: Any, channel_id: int) -> int | None:
    """id поста канала, если message — его автопересылка в чат обсуждений, иначе None."""
    fwd = getattr(message, "fwd_from", None)
    if fwd is None or not fwd.channel_post:
        return None
    # у автопересылки saved_from_peer указывает на канал; from_id бывает пустым у подписанных постов
    for peer in (fwd.saved_from_peer, fwd.from_id):
        if peer_channel_id(peer) == channel_id:
            return fwd.saved_from_msg_id or fwd.channel_post
    return None


def thread_key(discussion_message: Any) -> Hashable:
    """Один ключ на пост: альбом из нескольких сообщений — один пост."""
    if discussion_message.grouped_id:
        return ("album", discussion_message.grouped_id)
    fwd = discussion_message.fwd_from
    return ("post", (fwd.saved_from_msg_id or fwd.channel_post) if fwd else discussion_message.id)


def channel_key(post: Any) -> Hashable:
    return ("album", post.grouped_id) if post.grouped_id else ("post", post.id)


def discussion_top(messages: list[Any]) -> Any:
    """Из ответа GetDiscussionMessage: верхнее сообщение треда (у альбома — с меньшим id)."""
    return min(messages, key=lambda m: m.id)


class CommentPicker:
    """Выдаёт тексты по кругу в случайном порядке, без повтора подряд на стыке кругов."""

    def __init__(self, comments: tuple[str, ...], rng: random.Random | None = None) -> None:
        if not comments:
            raise ValueError("нужен хотя бы один комментарий")
        self._comments = list(comments)
        self._rng = rng or random.Random()
        self._queue: list[str] = []
        self._last: str | None = None
        self._refill()

    def _refill(self) -> None:
        self._queue = self._comments[:]
        self._rng.shuffle(self._queue)
        if len(self._queue) > 1 and self._queue[-1] == self._last:
            self._queue.reverse()

    def next(self) -> str:
        self._last = self._queue.pop()
        if not self._queue:
            self._refill()   # заранее, чтобы в момент отправки не тратить время
        return self._last
