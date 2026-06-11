from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    original_name TEXT,
    entry_date TEXT NOT NULL,
    expiry_date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class Item:
    id: int
    name: str
    entry_date: date
    expiry_date: date
    original_name: str | None = None


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        async with self._c.execute("PRAGMA table_info(items)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        if "original_name" not in cols:
            await self._c.execute("ALTER TABLE items ADD COLUMN original_name TEXT")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _c(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not connected"
        return self._conn

    async def list_items(self) -> list[Item]:
        async with self._c.execute(
            "SELECT id, name, entry_date, expiry_date, original_name "
            "FROM items ORDER BY expiry_date, id"
        ) as cur:
            rows = await cur.fetchall()
        return [
            Item(
                id=r[0],
                name=r[1],
                entry_date=date.fromisoformat(r[2]),
                expiry_date=date.fromisoformat(r[3]),
                original_name=r[4],
            )
            for r in rows
        ]

    async def add_item(
        self,
        name: str,
        entry: date,
        expiry: date,
        original_name: str | None = None,
    ) -> int:
        cur = await self._c.execute(
            "INSERT INTO items (name, original_name, entry_date, expiry_date) "
            "VALUES (?, ?, ?, ?)",
            (name, original_name, entry.isoformat(), expiry.isoformat()),
        )
        await self._c.commit()
        return cur.lastrowid or 0

    async def _oldest_id(self, name: str) -> int | None:
        async with self._c.execute(
            "SELECT id FROM items WHERE name = ? ORDER BY entry_date, id LIMIT 1",
            (name,),
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def consume_oldest(self, name: str) -> bool:
        item_id = await self._oldest_id(name)
        if item_id is None:
            return False
        await self._c.execute("DELETE FROM items WHERE id = ?", (item_id,))
        await self._c.commit()
        return True

    async def update_oldest(
        self,
        name: str,
        entry: date | None = None,
        expiry: date | None = None,
    ) -> bool:
        item_id = await self._oldest_id(name)
        if item_id is None:
            return False
        if entry is not None:
            await self._c.execute(
                "UPDATE items SET entry_date = ? WHERE id = ?",
                (entry.isoformat(), item_id),
            )
        if expiry is not None:
            await self._c.execute(
                "UPDATE items SET expiry_date = ? WHERE id = ?",
                (expiry.isoformat(), item_id),
            )
        await self._c.commit()
        return True

    async def get_kv(self, key: str) -> str | None:
        async with self._c.execute("SELECT value FROM kv WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_kv(self, key: str, value: str) -> None:
        await self._c.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await self._c.commit()
