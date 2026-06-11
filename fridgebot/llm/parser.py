import asyncio
import os
from datetime import date
from typing import Awaitable, Callable, Literal, TypeVar

import openai
from loguru import logger
from pydantic import BaseModel, Field, ValidationError

T = TypeVar("T")
M = TypeVar("M", bound=BaseModel)

MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
# 自定义 API 入口地址（任意 OpenAI 兼容端点）。留空则用官方 OpenAI 地址。
BASE_URL = os.getenv("OPENAI_BASE_URL") or None
RETRYABLE_CODES = {429, 500, 502, 503, 504}


class Operation(BaseModel):
    intent: Literal["add", "update", "eat", "finish", "discard"]
    item: str
    original_name: str | None = None
    entry_date: str | None = None
    expiry_date: str | None = None


class ParsedInput(BaseModel):
    kind: Literal["operations", "query", "unknown"]
    operations: list[Operation] = []
    confidence: float = Field(ge=0, le=1)
    reasoning: str


SYSTEM_PROMPT_RULES = """你是一个家用冰箱食材管理助手，解析用户输入并解析成特定格式。

一条消息可能包含多项操作，应当将其全部提取出来。

消息类型 (kind)：
- operations: 用户在对冰箱进行增/删/改（可包含多项）
- query: 用户在查询冰箱内容
- unknown: 无法判断 → 低置信度

每个 operation 字段：
- intent（务必区分"吃了"和"吃完了"）：
  - add: 新增食材（买了 / 加了）
  - update: 修改已有食材的名称或日期
  - eat: 用户说"吃了 X / 用了 X"但**没说吃完** → 记录吃过，但**不从冰箱移除**
  - finish: 用户说"X 吃完了 / 用完了 / 没了" → 记录吃过 **且从冰箱移除**
  - discard: 用户说"X 扔了 / 过期了 / 坏了 / 不要了" → 从冰箱移除，**不算吃过**
  - 同一食材若既说"吃了"又说"吃完了"，只输出一个 finish（finish 已含"吃过"语义），不要再输出 eat
- item: **中文**食材名称（单个，多个食材拆成多个 operation）
  - 规则见下方「命名规则」
- original_name: 可选，仅当 item 是从外文翻译而来时，填原文；否则 null
- entry_date（入库日期，YYYY-MM-DD）：
  - "今天买了" → 今天
  - "昨天买的" → 昨天
  - add 意图未提及 → 今天
  - eat / finish / discard / update 留 null
- expiry_date（过期日期，YYYY-MM-DD）：
  - 用户明说日期 → 按用户说的
  - "还能放 N 天" → 今天 + N
  - add 未提及 → 按「冷藏、未开封、家庭环境」给保守估计
  - eat / finish / discard 留 null

命名规则（决定 item 和 original_name）：
- 用户输入**全中文**（含"光明牛奶"这种中文品牌）→ item=用户原话，original_name=null
- 用户输入**纯外文**（如"Hänchen Oberschenkel"、"Yogurt"）→ item=中文翻译，original_name=原文
- 用户输入**中外混合**（如"Lay's 薯片"、"Haagen-Dazs 冰淇淋"）→ 由你判断：
  - 中文部分已能识别品类（"Lay's **薯片**"）→ item=原样保留，original_name=null
  - 中文部分不足以识别（如仅品牌名"Haagen-Dazs"无中文品类）→ item=中文品类（"冰淇淋"），original_name=原文

「冰箱现有」匹配规则（仅 eat / finish / discard / update）：
- 列表格式：单纯"A"表示 name=A 且无原文；"A (B)"表示 name=A、original_name=B
- 即使用户说的与列表中写法不同（中文别名 / 简称 / 使用原文名），只要语义明确对应某一项，**item 填列表中的 name 部分**；若该项带原文，同时输出 original_name=原文
- 示例：
  - 列表 ["鸡腿 (Hänchen Oberschenkel)"]，用户："Hänchen 吃完了" → finish, item="鸡腿", original_name="Hänchen Oberschenkel"
  - 列表 ["鸡腿 (Hänchen Oberschenkel)"]，用户："鸡腿吃完了" → finish, item="鸡腿", original_name="Hänchen Oberschenkel"
  - 列表 ["老酸奶"]，用户："酸奶扔了" → discard, item="老酸奶", original_name=null
- 列表中找不到语义对应项：item 按「命名规则」处理，reasoning 注明"冰箱中无此项"，confidence 降到 ≤ 0.6

多项操作示例：
- "今天买了生菜和一盒鸡蛋" → 2 个 add
- "今天吃了牛腱和土豆" → 2 个 eat（不移除）
- "牛奶喝完了，酸奶还能放 2 天" → 1 finish（牛奶）+ 1 update（酸奶）
- "今天吃了牛腱土豆，牛腱吃完了" → finish（牛腱）+ eat（土豆）
- "酸奶和生菜都扔了" → 2 个 discard
- "冰箱里还有啥" → kind=query, operations 为空
- "今天天气真好" → kind=unknown, operations 为空

confidence（整条消息整体置信度）：
- >= 0.9: 意图和字段都明确
- 0.7–0.9: 基本确定但有模糊
- < 0.7: 模棱两可，应让用户重说

reasoning: 简短说明判断依据，尤其保质期估计如何得出。

query / unknown 时 operations 为空数组 []。"""


JSON_FORMAT = """输出格式：只返回一个 JSON 对象，不要包含任何额外文字、解释或 markdown 代码块。结构如下：
{
  "kind": "operations" | "query" | "unknown",
  "confidence": 0~1 之间的数字,
  "reasoning": "字符串",
  "operations": [
    {
      "intent": "add" | "update" | "eat" | "finish" | "discard",
      "item": "字符串",
      "original_name": 字符串或 null,
      "entry_date": "YYYY-MM-DD" 或 null,
      "expiry_date": "YYYY-MM-DD" 或 null
    }
  ]
}"""


def _build_user_prompt(today: date, user_input: str, existing_items: list[str] | None) -> str:
    lines = [f"今天：{today.isoformat()}"]
    if existing_items:
        lines.append(f"冰箱现有：{', '.join(existing_items)}")
    lines.append(f'用户输入："{user_input}"')
    return "\n".join(lines)


def _client() -> openai.AsyncOpenAI:
    return openai.AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], base_url=BASE_URL)


# 输出格式策略：默认 json_object（prompt 描述 schema + pydantic 校验，端点兼容性最广）；
# 置 LLM_STRUCTURED_OUTPUT=true 切换为 json_schema 约束解码（更强格式保证，需端点支持）。
# 两套实现都保留在 complete() 里，靠环境变量切换，回退无需改代码。
STRUCTURED_OUTPUT = os.getenv("LLM_STRUCTURED_OUTPUT", "").strip().lower() in ("1", "true", "yes", "on")


def system_content(rules: str, json_format: str) -> str:
    """组装 system 提示：structured 模式只发规则段；json_object 模式追加 JSON 格式说明。
    避免在 structured 模式下与 response_format 重复描述形状，省冗余 token。"""
    return rules if STRUCTURED_OUTPUT else f"{rules}\n\n{json_format}"


async def complete(messages: list, schema: type[M], model: str | None = None) -> M:
    """统一 LLM 出口：按 STRUCTURED_OUTPUT 选择约束解码或 json_object+校验，返回校验后的 schema 实例。"""
    client = _client()
    model = model or MODEL
    if STRUCTURED_OUTPUT:
        response = await client.chat.completions.parse(
            model=model,
            messages=messages,
            response_format=schema,
            temperature=0.1,
        )
        parsed = response.choices[0].message.parsed
        if parsed is None:
            raise ValueError("structured output 为空（refusal 或截断）")
        return parsed
    response = await client.chat.completions.create(
        model=model,
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.1,
    )
    return schema.model_validate_json(response.choices[0].message.content or "")


async def parse_input(
    user_input: str,
    today: date | None = None,
    existing_items: list[str] | None = None,
) -> ParsedInput:
    today = today or date.today()
    user_prompt = _build_user_prompt(today, user_input, existing_items)
    messages = [
        {"role": "system", "content": system_content(SYSTEM_PROMPT_RULES, JSON_FORMAT)},
        {"role": "user", "content": user_prompt},
    ]
    return await complete(messages, ParsedInput)


def _retry_after(exc: openai.APIStatusError, default: float) -> float:
    header = exc.response.headers.get("retry-after")
    try:
        return float(header) if header else default
    except (TypeError, ValueError):
        return default


async def run_with_retry(
    call: Callable[[], Awaitable[T]],
    max_attempts: int = 5,
    base_delay: float = 2.0,
) -> T:
    """通用重试包装：限速(429)/5xx 按 retry-after 退避，连接错误/JSON 校验失败也重试。"""
    delay = base_delay
    for attempt in range(1, max_attempts + 1):
        last = attempt == max_attempts
        try:
            return await call()
        except openai.APIStatusError as e:
            if e.status_code not in RETRYABLE_CODES or last:
                raise
            wait = _retry_after(e, default=delay)
            logger.warning(f"llm {e.status_code}, retry in {wait:.1f}s (attempt {attempt}/{max_attempts})")
            await asyncio.sleep(wait)
            delay = min(delay * 2, 60)
        except (openai.APIConnectionError, ValidationError) as e:
            if last:
                raise
            logger.warning(f"llm {type(e).__name__}, retry in {delay:.1f}s (attempt {attempt}/{max_attempts})")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
    raise RuntimeError("unreachable")


async def parse_with_retry(
    user_input: str,
    today: date | None = None,
    existing_items: list[str] | None = None,
    max_attempts: int = 5,
    base_delay: float = 2.0,
) -> ParsedInput:
    return await run_with_retry(
        lambda: parse_input(user_input, today, existing_items),
        max_attempts=max_attempts,
        base_delay=base_delay,
    )
