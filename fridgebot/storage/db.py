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
    expiry_date TEXT,
    price_cents INTEGER,
    batch_id TEXT,
    category TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    eaten_date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meal_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meal_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    original_name TEXT,
    price_cents INTEGER
);
-- 不可变购买流水：记录每笔花销（与 items 库存解耦，消费删库存不影响这里）。
-- 统计时按 purchase_date 区间 SUM 现算，支持"某月伙食费"之类查询。
CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_date TEXT NOT NULL,
    name TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    batch_id TEXT
);
"""


@dataclass
class Item:
    id: int
    name: str
    entry_date: date
    # 保质期可为空：未知/未估算（如发票录入但未运行 /estimate）。空 ≠ 当天过期。
    expiry_date: date | None
    original_name: str | None = None
    # 单价（分），收据流写入；文字流为 None。整数避免成本累加浮点误差。
    price_cents: int | None = None
    # 入库批次：一张收据=一个 batch_id，用于撤销与未来按单/按饭归集。
    batch_id: str | None = None
    # 'fresh' / 'frozen'，仅收据流标记。
    category: str | None = None


@dataclass
class MealItem:
    name: str
    original_name: str | None = None
    # 吃下时该食材的购入单价快照（分）；无价记 None。为未来"每顿饭成本"留底。
    price_cents: int | None = None


@dataclass
class Meal:
    id: int
    eaten_date: date
    items: list[MealItem]


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
            info = await cur.fetchall()
        cols = {row[1] for row in info}
        if "original_name" not in cols:
            await self._c.execute("ALTER TABLE items ADD COLUMN original_name TEXT")
        if "price_cents" not in cols:
            await self._c.execute("ALTER TABLE items ADD COLUMN price_cents INTEGER")
        if "batch_id" not in cols:
            await self._c.execute("ALTER TABLE items ADD COLUMN batch_id TEXT")
        if "category" not in cols:
            await self._c.execute("ALTER TABLE items ADD COLUMN category TEXT")
        # 旧库 expiry_date 建表时是 NOT NULL；SQLite 无法直接改列约束，需重建表使其可空。
        # PRAGMA table_info 行格式：(cid, name, type, notnull, dflt_value, pk)
        expiry_notnull = next((r[3] for r in info if r[1] == "expiry_date"), 0)
        if expiry_notnull:
            await self._make_expiry_nullable()

    async def _make_expiry_nullable(self) -> None:
        await self._c.executescript(
            """
            CREATE TABLE items_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                original_name TEXT,
                entry_date TEXT NOT NULL,
                expiry_date TEXT,
                price_cents INTEGER,
                batch_id TEXT,
                category TEXT
            );
            INSERT INTO items_new
                (id, name, original_name, entry_date, expiry_date, price_cents, batch_id, category)
                SELECT id, name, original_name, entry_date, expiry_date, price_cents, batch_id, category
                FROM items;
            DROP TABLE items;
            ALTER TABLE items_new RENAME TO items;
            """
        )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _c(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not connected"
        return self._conn

    _COLUMNS = "id, name, entry_date, expiry_date, original_name, price_cents, batch_id, category"

    @staticmethod
    def _row_to_item(r) -> Item:
        return Item(
            id=r[0],
            name=r[1],
            entry_date=date.fromisoformat(r[2]),
            expiry_date=date.fromisoformat(r[3]) if r[3] else None,
            original_name=r[4],
            price_cents=r[5],
            batch_id=r[6],
            category=r[7],
        )

    async def list_items(self) -> list[Item]:
        async with self._c.execute(
            f"SELECT {self._COLUMNS} FROM items ORDER BY expiry_date, id"
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_item(r) for r in rows]

    async def add_item(
        self,
        name: str,
        entry: date,
        expiry: date | None,
        original_name: str | None = None,
        price_cents: int | None = None,
        batch_id: str | None = None,
        category: str | None = None,
    ) -> int:
        cur = await self._c.execute(
            "INSERT INTO items "
            "(name, original_name, entry_date, expiry_date, price_cents, batch_id, category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                original_name,
                entry.isoformat(),
                expiry.isoformat() if expiry else None,
                price_cents,
                batch_id,
                category,
            ),
        )
        await self._c.commit()
        return cur.lastrowid or 0

    async def list_batch(self, batch_id: str) -> list[Item]:
        async with self._c.execute(
            f"SELECT {self._COLUMNS} FROM items WHERE batch_id = ? ORDER BY id",
            (batch_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_item(r) for r in rows]

    async def delete_batch(self, batch_id: str) -> int:
        cur = await self._c.execute(
            "DELETE FROM items WHERE batch_id = ?", (batch_id,)
        )
        await self._c.commit()
        return cur.rowcount or 0

    async def list_unknown_expiry(self) -> list[Item]:
        async with self._c.execute(
            f"SELECT {self._COLUMNS} FROM items WHERE expiry_date IS NULL ORDER BY id"
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_item(r) for r in rows]

    async def set_expiry(self, item_id: int, expiry: date) -> None:
        await self._c.execute(
            "UPDATE items SET expiry_date = ? WHERE id = ?",
            (expiry.isoformat(), item_id),
        )
        await self._c.commit()

    async def _oldest_id(self, name: str) -> int | None:
        async with self._c.execute(
            "SELECT id FROM items WHERE name = ? ORDER BY entry_date, id LIMIT 1",
            (name,),
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def oldest_item(self, name: str) -> Item | None:
        async with self._c.execute(
            f"SELECT {self._COLUMNS} FROM items WHERE name = ? ORDER BY entry_date, id LIMIT 1",
            (name,),
        ) as cur:
            row = await cur.fetchone()
        return self._row_to_item(row) if row else None

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

    # ---- 餐食记录 ----

    async def add_meal(self, eaten_date: date, items: list[MealItem]) -> int:
        cur = await self._c.execute(
            "INSERT INTO meals (eaten_date) VALUES (?)", (eaten_date.isoformat(),)
        )
        meal_id = cur.lastrowid or 0
        for it in items:
            await self._c.execute(
                "INSERT INTO meal_items (meal_id, name, original_name, price_cents) "
                "VALUES (?, ?, ?, ?)",
                (meal_id, it.name, it.original_name, it.price_cents),
            )
        await self._c.commit()
        return meal_id

    @staticmethod
    def _date_range_clause(column: str, start: date | None, end: date | None) -> tuple[str, list[str]]:
        conds, params = [], []
        if start is not None:
            conds.append(f"{column} >= ?")
            params.append(start.isoformat())
        if end is not None:
            conds.append(f"{column} <= ?")
            params.append(end.isoformat())
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        return where, params

    async def count_meals(self, start: date | None = None, end: date | None = None) -> int:
        where, params = self._date_range_clause("eaten_date", start, end)
        async with self._c.execute(f"SELECT COUNT(*) FROM meals{where}", params) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    async def recent_meals(self, limit: int = 5) -> list[Meal]:
        async with self._c.execute(
            "SELECT id, eaten_date FROM meals ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            meal_rows = await cur.fetchall()
        meals: list[Meal] = []
        for mid, eaten in meal_rows:
            async with self._c.execute(
                "SELECT name, original_name, price_cents FROM meal_items "
                "WHERE meal_id = ? ORDER BY id",
                (mid,),
            ) as cur:
                item_rows = await cur.fetchall()
            items = [MealItem(name=r[0], original_name=r[1], price_cents=r[2]) for r in item_rows]
            meals.append(Meal(id=mid, eaten_date=date.fromisoformat(eaten), items=items))
        return meals

    # ---- 购买流水（不可变；统计时现算，与库存增删解耦） ----

    async def add_purchase(
        self, purchase_date: date, name: str, amount_cents: int, batch_id: str | None = None
    ) -> int:
        cur = await self._c.execute(
            "INSERT INTO purchases (purchase_date, name, amount_cents, batch_id) "
            "VALUES (?, ?, ?, ?)",
            (purchase_date.isoformat(), name, amount_cents, batch_id),
        )
        await self._c.commit()
        return cur.lastrowid or 0

    async def delete_purchases(self, batch_id: str) -> int:
        cur = await self._c.execute(
            "DELETE FROM purchases WHERE batch_id = ?", (batch_id,)
        )
        await self._c.commit()
        return cur.rowcount or 0

    async def spend_cents(self, start: date | None = None, end: date | None = None) -> int:
        """统计 [start, end] 区间的花销（含端点）；不传则全时段。现算求和。"""
        where, params = self._date_range_clause("purchase_date", start, end)
        async with self._c.execute(
            f"SELECT COALESCE(SUM(amount_cents), 0) FROM purchases{where}", params
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0
