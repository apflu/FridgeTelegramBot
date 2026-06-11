import asyncio
import os
from datetime import date

from dotenv import load_dotenv

from fridgebot import logging_config as logconfig

load_dotenv()
logconfig.setup()

from fridgebot.llm import parse_with_retry

# 两次调用间隔（秒），按 provider 限速调整。
# Gemini 免费 gemini-2.5-flash 约 10 RPM → 6s 足够；付费 OpenAI 兼容端点可设 0。
SLEEP_BETWEEN = float(os.getenv("TEST_SLEEP_BETWEEN", "6"))
TODAY = date(2026, 4, 24)

SAMPLES: list[tuple[str, list[str] | None]] = [
    ("今天买了一颗生菜", None),
    ("今天买了一颗生菜和一盒鸡蛋", None),
    ("牛奶还能放 3 天", ["牛奶"]),
    ("酸奶过期了扔了", ["酸奶", "生菜"]),
    ("鸡蛋改到下周三过期", ["鸡蛋", "生菜"]),
    ("牛奶喝完了，酸奶还能放 2 天", ["牛奶", "酸奶"]),
    ("酸奶和生菜都扔了", ["酸奶", "生菜", "鸡蛋"]),
    ("冰箱里还有啥", ["鸡蛋", "生菜"]),
    ("今天天气真好", None),
]


async def run():
    for i, (text, existing) in enumerate(SAMPLES):
        if i > 0:
            await asyncio.sleep(SLEEP_BETWEEN)
        print(f"\n>>> {text}" + (f"  [已有: {existing}]" if existing else ""))
        try:
            r = await parse_with_retry(text, today=TODAY, existing_items=existing)
            conf_flag = "" if r.confidence >= 0.7 else "  ⚠ LOW"
            print(f"    kind:       {r.kind}  (conf={r.confidence:.2f}){conf_flag}")
            for j, op in enumerate(r.operations):
                parts = [op.intent, op.item]
                if op.entry_date:
                    parts.append(f"entry={op.entry_date}")
                if op.expiry_date:
                    parts.append(f"expiry={op.expiry_date}")
                print(f"    op[{j}]:      {'  '.join(parts)}")
            print(f"    reasoning:  {r.reasoning}")
        except Exception as e:
            print(f"    ERROR: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(run())
