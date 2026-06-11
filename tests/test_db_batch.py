"""离线单测（无需 API）：验证 price_cents / batch_id 的数据模型自洽性。

运行： uv run python -m tests.test_db_batch
"""

import asyncio
import os
import tempfile
from datetime import date

from fridgebot.storage import Database

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

    await db.close()
    print("OK: list_batch / delete_batch / price_cents 全部断言通过")


if __name__ == "__main__":
    asyncio.run(main())
