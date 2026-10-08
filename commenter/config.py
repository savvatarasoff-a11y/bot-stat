"""Настройки комментатора из переменных окружения (или файла .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from bot.config import load_dotenv


def parse_comments(raw: str) -> tuple[str, ...]:
    """"Первый! | Топ" -> ("Первый!", "Топ"); "\\n" внутри — перенос строки."""
    return tuple(c.strip().replace("\\n", "\n") for c in raw.split("|") if c.strip())


def read_comments_file(path: str) -> tuple[str, ...]:
    """Комментарии в файле разделяются пустой строкой — так можно писать многострочные."""
    with open(path, encoding="utf-8") as f:
        return tuple(b.strip() for b in f.read().replace("\r\n", "\n").split("\n\n") if b.strip())


def parse_channel(raw: str) -> str | int:
    raw = raw.strip().removeprefix("https://").removeprefix("http://").removeprefix("t.me/")
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw if raw.startswith("@") else f"@{raw}"


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    channel: str | int
    comments: tuple[str, ...]
    session: str = "data/commenter"     # файл сессии или строка StringSession
    delay: float = 0                    # пауза перед отправкой, сек
    poll_interval: float = 0            # опрос канала в дополнение к апдейтам, сек; 0 — выкл
    join: bool = True                   # вступить в канал и чат обсуждений, если ещё не там

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        env = os.environ.get
        missing = [k for k in ("API_ID", "API_HASH", "CHANNEL") if not env(k, "").strip()]
        if missing:
            raise SystemExit(f"Не заданы {', '.join(missing)} (см. .env.example)")
        comments = parse_comments(env("COMMENTS", ""))
        if path := env("COMMENTS_FILE", "").strip():
            comments += read_comments_file(path)
        if not comments:
            raise SystemExit("Не задан текст: COMMENTS или COMMENTS_FILE (см. .env.example)")
        return cls(
            api_id=int(env("API_ID")),
            api_hash=env("API_HASH").strip(),
            channel=parse_channel(env("CHANNEL")),
            comments=comments,
            session=env("SESSION", "").strip() or "data/commenter",
            delay=float(env("COMMENT_DELAY", 0)),
            poll_interval=float(env("POLL_INTERVAL", 0)),
            join=env("AUTO_JOIN", "1").strip().lower() not in ("0", "false", "no", ""),
        )
