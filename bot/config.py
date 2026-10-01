"""Настройки из переменных окружения (или файла .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_ADMIN_IDS = "5349009098"


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.split(" #")[0].strip())


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    db_path: str = "data/bot.db"
    broadcast_rate: float = 25

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        token = os.environ.get("BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit("Не задан BOT_TOKEN (см. .env.example)")
        ids = os.environ.get("ADMIN_IDS") or DEFAULT_ADMIN_IDS
        return cls(
            bot_token=token,
            admin_ids=frozenset(int(x) for x in ids.replace(" ", "").split(",") if x),
            db_path=os.environ.get("DB_PATH", "data/bot.db"),
            broadcast_rate=float(os.environ.get("BROADCAST_RATE", 25)),
        )
