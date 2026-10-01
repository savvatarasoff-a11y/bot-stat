"""SQLite: все чаты бота (личные, группы, каналы) и история рассылок."""
from __future__ import annotations

import os
import time
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id         INTEGER PRIMARY KEY,
    type       TEXT NOT NULL,              -- private / group / supergroup / channel
    title      TEXT,                       -- имя пользователя или название группы
    username   TEXT,
    start_arg  TEXT,                       -- параметр /start (откуда пришёл)
    active     INTEGER NOT NULL DEFAULT 1, -- 0: заблокировал бота / бот удалён из чата
    created_at REAL NOT NULL,
    last_seen  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS chats_active ON chats(active, id);
CREATE TABLE IF NOT EXISTS broadcasts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id    INTEGER NOT NULL,
    started_at  REAL NOT NULL,
    finished_at REAL,
    sent        INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'running'   -- running / done / stopped
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if os.path.dirname(self.path):
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(SCHEMA)
        # рассылки, оборванные перезапуском, больше не «идут»
        await self._conn.execute("UPDATE broadcasts SET status='stopped' WHERE status='running'")
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "База не подключена"
        return self._conn

    async def one(self, sql: str, *args: Any) -> dict | None:
        async with self.conn.execute(sql, args) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def all(self, sql: str, *args: Any) -> list[dict]:
        async with self.conn.execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ---------- чаты ----------

    async def upsert_chat(self, chat_id: int, type_: str, title: str | None, username: str | None,
                          start_arg: str | None = None) -> bool:
        """Запоминает чат и помечает активным. True — чат новый."""
        now = time.time()
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO chats(id, type, title, username, start_arg, created_at, last_seen) "
            "VALUES (?,?,?,?,?,?,?)", (chat_id, type_, title, username, start_arg, now, now))
        created = cur.rowcount == 1
        if not created:
            await self.conn.execute(
                "UPDATE chats SET type=?, title=?, username=?, active=1, last_seen=? WHERE id=?",
                (type_, title, username, now, chat_id))
        await self.conn.commit()
        return created

    async def set_inactive(self, chat_ids: list[int]) -> None:
        if chat_ids:
            await self.conn.executemany("UPDATE chats SET active=0 WHERE id=?", [(c,) for c in chat_ids])
            await self.conn.commit()

    async def active_chat_ids(self) -> list[int]:
        return [r["id"] for r in await self.all("SELECT id FROM chats WHERE active=1 ORDER BY id")]

    async def stats(self) -> dict:
        now = time.time()
        row = await self.one(
            "SELECT COUNT(*) total, COALESCE(SUM(active),0) active, "
            "COALESCE(SUM(type='private'),0) users, COALESCE(SUM(type='private' AND active=1),0) users_active, "
            "COALESCE(SUM(type!='private'),0) groups, COALESCE(SUM(type!='private' AND active=1),0) groups_active, "
            "COALESCE(SUM(created_at>?),0) day, COALESCE(SUM(created_at>?),0) week, "
            "COALESCE(SUM(last_seen>?),0) seen_day FROM chats",
            now - 86400, now - 7 * 86400, now - 86400)
        return row

    async def top_sources(self, limit: int = 10) -> list[dict]:
        return await self.all(
            "SELECT start_arg, COUNT(*) n FROM chats WHERE start_arg IS NOT NULL AND start_arg != '' "
            "GROUP BY start_arg ORDER BY n DESC LIMIT ?", limit)

    # ---------- рассылки ----------

    async def broadcast_begin(self, admin_id: int) -> int:
        cur = await self.conn.execute("INSERT INTO broadcasts(admin_id, started_at) VALUES (?,?)",
                                      (admin_id, time.time()))
        await self.conn.commit()
        return cur.lastrowid

    async def broadcast_end(self, bc_id: int, sent: int, failed: int, status: str) -> None:
        await self.conn.execute("UPDATE broadcasts SET finished_at=?, sent=?, failed=?, status=? WHERE id=?",
                                (time.time(), sent, failed, status, bc_id))
        await self.conn.commit()

    async def last_broadcasts(self, limit: int = 5) -> list[dict]:
        return await self.all("SELECT * FROM broadcasts ORDER BY id DESC LIMIT ?", limit)
