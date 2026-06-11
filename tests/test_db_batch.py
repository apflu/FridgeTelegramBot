"""离线单测（无需 API）：验证 price_cents / batch_id 的数据模型自洽性。

运行： uv run python -m tests.test_db_batch
"""

import asyncio
import os
import tempfile
from datetime import date

import aiosqlite

from fridgebot.storage import Database, MealItem

TODAY = date(2026, 4, 24)
EXPIRY = date(2026, 5, 1)


async def main() -> None:
    tmp = tempfile.mkdtemp()
    db = Database(os.path.join(tmp, "test.db"))
    await db.connect()

    # 收据「酸奶 ×3 @ €0.79」展开成 3 行，每行单价 79 分，同一 batch。
    b1 = "batch_one_01"
    for _ in range(3):
        await db.add_item(
            "酸奶", TODAY, EXPIRY, original_name="Joghurt",
            price_cents=79, batch_id=b1, category="fresh",
        )
    # 按重量项「鸡腿 0.5kg €4.50」：quantity=1，单价=总价。
    b2 = "batch_two_02"
    await db.add_item(
        "鸡腿 0.5kg", TODAY, EXPIRY, original_name="Hähnchenschenkel",
        price_cents=450, batch_id=b2, category="fresh",
    )
    # 文字流入库：无 price/batch（向后兼容）。
    await db.add_item("生菜", TODAY, EXPIRY)

    items = await db.list_items()
    assert len(items) == 5, f"expected 5 items, got {len(items)}"

    batch1 = await db.list_batch(b1)
    assert len(batch1) == 3, f"batch1 should have 3 rows, got {len(batch1)}"
    s = sum(i.price_cents for i in batch1)
    assert s == 237, f"batch1 price sum should be 237, got {s}"
    assert all(i.category == "fresh" for i in batch1)
    assert all(i.original_name == "Joghurt" for i in batch1)

    # 文字流那行价格/批次应为 None。
    lettuce = next(i for i in items if i.name == "生菜")
    assert lettuce.price_cents is None and lettuce.batch_id is None

    # 撤销 batch1：删 3 行，返回行数 3。
    removed = await db.delete_batch(b1)
    assert removed == 3, f"delete_batch should remove 3, got {removed}"
    items = await db.list_items()
    assert len(items) == 2, f"after undo expected 2 items, got {len(items)}"
    assert await db.list_batch(b1) == []

    # batch2（鸡腿）仍在，价格完整。
    chicken = await db.list_batch(b2)
    assert len(chicken) == 1 and chicken[0].price_cents == 450

    # 空保质期（发票流）：能入库、出现在 list_unknown_expiry、且 set_expiry 后转为已知。
    onion_id = await db.add_item("洋葱", TODAY, None, price_cents=88, batch_id="b3", category="fresh")
    unknown = await db.list_unknown_expiry()
    assert [i.id for i in unknown] == [onion_id], "应只有洋葱保质期未知"
    assert unknown[0].expiry_date is None
    await db.set_expiry(onion_id, date(2026, 5, 3))
    assert await db.list_unknown_expiry() == [], "set_expiry 后不应再有未知项"

    # 购买流水：现算求和 + 按日期区间过滤 + 按 batch 撤销。
    await db.add_purchase(date(2026, 2, 10), "牛肉", 676, batch_id="bp1")
    await db.add_purchase(date(2026, 2, 20), "土豆", 279, batch_id="bp1")
    await db.add_purchase(date(2026, 3, 5), "生菜", 88, batch_id="bp2")
    assert await db.spend_cents() == 676 + 279 + 88, "全时段合计应为 1043"
    feb = await db.spend_cents(date(2026, 2, 1), date(2026, 2, 28))
    assert feb == 676 + 279, f"2月应为 955，得到 {feb}"
    await db.delete_purchases("bp1")  # 撤销 2 月那单
    assert await db.spend_cents() == 88, "撤销后只剩生菜 88"
    assert await db.spend_cents(date(2026, 2, 1), date(2026, 2, 28)) == 0

    # 餐食记录：餐数 + 内容 + 最近在前。
    await db.add_meal(date(2026, 6, 11), [MealItem("牛腱肉片", "JB-BEINSCHEIBE", 676), MealItem("土豆", None, 279)])
    await db.add_meal(date(2026, 6, 11), [MealItem("生菜")])
    assert await db.count_meals() == 2, "应有 2 餐"
    recent = await db.recent_meals(limit=5)
    assert recent[0].items[0].name == "生菜", "最近一餐应在最前"
    assert len(recent[1].items) == 2 and recent[1].items[0].price_cents == 676

    await db.close()
    print("OK: batch / price / 空保质期 / 购买流水(含区间) / 餐食记录 全部断言通过")


async def test_migration() -> None:
    """旧库 expiry_date NOT NULL 且缺新列 → 打开后应迁移为可空并保留数据。"""
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "old.db")
    conn = await aiosqlite.connect(path)
    await conn.executescript(
        """
        CREATE TABLE items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            entry_date TEXT NOT NULL,
            expiry_date TEXT NOT NULL
        );
        INSERT INTO items (name, entry_date, expiry_date)
            VALUES ('旧货', '2026-04-01', '2026-04-10');
        """
    )
    await conn.commit()
    await conn.close()

    db = Database(path)
    await db.connect()  # 触发 _migrate：ADD COLUMN ×4 + 重建表去 NOT NULL
    items = await db.list_items()
    assert len(items) == 1 and items[0].name == "旧货", "旧数据应保留"
    assert items[0].expiry_date == date(2026, 4, 10)
    # 迁移前这条会因 NOT NULL 失败；迁移后应成功。
    await db.add_item("无期限", date(2026, 4, 24), None)
    unknown = await db.list_unknown_expiry()
    assert len(unknown) == 1 and unknown[0].name == "无期限"
    await db.close()

    # 幂等：再次连接不应再重建、不报错。
    db2 = Database(path)
    await db2.connect()
    assert len(await db2.list_items()) == 2
    await db2.close()
    print("OK: 旧库 NOT NULL → 可空 迁移成功且幂等")


if __name__ == "__main__":
    asyncio.run(main())
    asyncio.run(test_migration())
