import base64
import os
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from .parser import MODEL, _client, run_with_retry

# 视觉模型：默认回退到文字模型（当前两者都是 gemini-2.5-flash，皆多模态）。
VISION_MODEL = os.getenv("OPENAI_VISION_MODEL") or MODEL


class ReceiptLine(BaseModel):
    name: str                          # 中文食材名
    original_name: str | None = None   # 收据原文（如德文）
    unit_price_cents: int              # 单价（分）；按重量项见 prompt 规则
    quantity: int = 1                  # 件数（入库时展开成 N 行）
    expiry_date: str                   # 保守估计 YYYY-MM-DD
    category: Literal["fresh", "frozen"]


class ParsedReceipt(BaseModel):
    lines: list[ReceiptLine] = []
    currency: str = "EUR"
    confidence: float = Field(ge=0, le=1)
    reasoning: str


RECEIPT_SYSTEM_PROMPT = """你是一个家用冰箱食材管理助手，从一张超市收据照片中抽取**需要烹饪的生鲜和速冻食品**。

只保留这两类，其余一律丢弃（不要出现在 lines 中）：
- fresh（生鲜）：蔬菜、水果、肉、鱼、蛋、奶/酸奶、豆腐等需冷藏或当季食用的食材
- frozen（速冻）：冷冻区商品（标签常见 TK / Tiefkühl / gefroren / frozen）

**必须丢弃**：零食、薯片、糖果、巧克力、饼干、饮料、酒、咖啡、茶、调味料/酱/油/盐糖、面包烘焙、罐头、以及一切非食品（纸巾、洗涤剂、塑料袋、押金 Pfand 等）。

每个保留项（ReceiptLine）字段：
- name：**中文**食材名。原文为外文时翻译成中文（如 Hähnchenschenkel → 鸡腿，Salat → 生菜）
- original_name：收据上的原文；name 本就是中文则为 null
- category："fresh" 或 "frozen"
- quantity：件数（整数）
- unit_price_cents：**单价**，单位是**分**（€0.79 → 79，€4.50 → 450）
- expiry_date：保质期，YYYY-MM-DD

价格与数量规则：
- 按件计价（如「酸奶 3 × €0.79」）：quantity=3，unit_price_cents=79（单价，不是总价）
- 按重量计价（如「0.5kg 鸡腿 €4.50」）：无法拆成整数件 → quantity=1，unit_price_cents=450（即实付总价），并把重量写进 name（「鸡腿 0.5kg」）
- 收据只给了某项的合计且件数>1，则 unit_price_cents = 合计 / 件数（四舍五入到整数分）

保质期估计（按「冷藏 / 冷冻、未开封、家庭环境」保守估计，以收据日期为今天）：
- 叶菜/生菜 3–5 天；其他蔬果 5–7 天
- 生肉/鱼 2–3 天；蛋 2–3 周；奶/酸奶按常规 1–2 周
- 速冻 30–90 天

confidence（整张收据识别的整体置信度，0–1）：识别清晰且金额明确 → 高；模糊/反光/看不清 → 低。

reasoning：简短说明，尤其哪些项被丢弃、保质期如何估计。

输出格式：只返回一个 JSON 对象，不要任何额外文字或 markdown 代码块。结构：
{
  "lines": [
    {
      "name": "字符串",
      "original_name": 字符串或 null,
      "category": "fresh" | "frozen",
      "quantity": 整数,
      "unit_price_cents": 整数,
      "expiry_date": "YYYY-MM-DD"
    }
  ],
  "currency": "EUR",
  "confidence": 0~1 的数字,
  "reasoning": "字符串"
}
没有任何生鲜/速冻项时 lines 为空数组 []。"""


async def parse_receipt(image_bytes: bytes, today: date | None = None) -> ParsedReceipt:
    today = today or date.today()
    b64 = base64.b64encode(image_bytes).decode()
    data_url = f"data:image/jpeg;base64,{b64}"

    response = await _client().chat.completions.create(
        model=VISION_MODEL,
        messages=[
            {"role": "system", "content": RECEIPT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"今天（收据日期）：{today.isoformat()}。识别这张超市收据。"},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
    )
    content = response.choices[0].message.content or ""
    return ParsedReceipt.model_validate_json(content)


async def parse_receipt_with_retry(
    image_bytes: bytes,
    today: date | None = None,
    max_attempts: int = 5,
    base_delay: float = 2.0,
) -> ParsedReceipt:
    return await run_with_retry(
        lambda: parse_receipt(image_bytes, today),
        max_attempts=max_attempts,
        base_delay=base_delay,
    )
