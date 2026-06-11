from datetime import date

from pydantic import BaseModel

from .parser import MODEL, _client, run_with_retry


class ExpiryGuess(BaseModel):
    index: int          # 对应输入列表的编号
    expiry_date: str    # YYYY-MM-DD


class ExpiryGuesses(BaseModel):
    guesses: list[ExpiryGuess] = []


ESTIMATE_SYSTEM_PROMPT = """你是一个家用冰箱食材保质期估算助手。
给定若干食材（含名称和入库日期），为每一项估算一个合理的过期日期。

估算规则（按「冷藏 / 冷冻、未开封、家庭环境」保守估计，从该项的入库日期算起）：
- 叶菜/生菜 3–5 天；其他蔬菜 5–7 天；水果 5–10 天
- 生肉/生鱼 2–3 天；香肠/熟食 5–7 天；蛋 2–3 周；奶/酸奶 1–2 周；豆腐 5–7 天
- 速冻食品 30–90 天
- 不确定时偏保守（取较短）

输出格式：只返回一个 JSON 对象，不要任何额外文字或 markdown：
{
  "guesses": [
    {"index": 整数, "expiry_date": "YYYY-MM-DD"}
  ]
}
index 必须与输入列表的编号一一对应，且每一项都要给出。"""


# entries: list[(name, original_name, entry_date)]
Entry = tuple[str, str | None, date]


def _build_prompt(entries: list[Entry], today: date) -> str:
    lines = [f"今天：{today.isoformat()}", "待估算食材："]
    for i, (name, original_name, entry) in enumerate(entries):
        disp = f"{name} ({original_name})" if original_name else name
        lines.append(f"[{i}] {disp}，入库 {entry.isoformat()}")
    return "\n".join(lines)


async def estimate_expiry(entries: list[Entry], today: date | None = None) -> ExpiryGuesses:
    today = today or date.today()
    prompt = _build_prompt(entries, today)
    response = await _client().chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": ESTIMATE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
    )
    return ExpiryGuesses.model_validate_json(response.choices[0].message.content or "")


async def estimate_expiry_with_retry(
    entries: list[Entry],
    today: date | None = None,
    max_attempts: int = 5,
    base_delay: float = 2.0,
) -> ExpiryGuesses:
    return await run_with_retry(
        lambda: estimate_expiry(entries, today),
        max_attempts=max_attempts,
        base_delay=base_delay,
    )
