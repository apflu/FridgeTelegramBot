import asyncio
from datetime import date

from dotenv import load_dotenv

from fridgebot import logging_config as logconfig

load_dotenv()
logconfig.setup()

from fridgebot.llm import ParseQueue

TODAY = date(2026, 4, 24)


async def main():
    q = ParseQueue(min_interval=13)
    await q.start()

    inputs = [
        ("今天买了一颗生菜", None),
        ("昨天买了两盒酸奶", None),
        ("吃掉了生菜", ["生菜", "酸奶"]),
    ]
    start = asyncio.get_running_loop().time()
    tasks = [q.submit(text, today=TODAY, existing_items=existing) for text, existing in inputs]
    print(f"Submitted {len(tasks)} concurrently; queue + min_interval=13s will serialize them.\n")

    for i, coro in enumerate(asyncio.as_completed(tasks)):
        r = await coro
        t = asyncio.get_running_loop().time() - start
        print(f"[t={t:5.1f}s] #{i} kind={r.kind} ops={[(o.intent, o.item) for o in r.operations]}")

    await q.stop()


if __name__ == "__main__":
    asyncio.run(main())
