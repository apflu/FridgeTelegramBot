from datetime import date, timedelta

from fridgebot.llm import ReceiptLine
from fridgebot.storage import Item, Meal

_CATEGORY_ICON = {"fresh": "🥬", "frozen": "❄️"}


def _money(cents: int, currency: str) -> str:
    return f"{currency}{cents / 100:.2f}"


def render_stats(
    spend_cents: int,
    meal_count: int,
    currency: str = "€",
    recent: list[Meal] | None = None,
) -> str:
    avg = _money(spend_cents // meal_count, currency) if meal_count else "—"
    lines = [
        "📊 开销统计",
        "",
        f"总开销：{_money(spend_cents, currency)}",
        f"餐数：{meal_count}",
        f"平均每餐：{avg}",
    ]
    if recent:
        lines.extend(["", "最近几餐："])
        for m in recent:
            names = "、".join(
                f"{it.name} ({it.original_name})" if it.original_name else it.name
                for it in m.items
            )
            lines.append(f"🍽 {m.eaten_date.strftime('%m-%d')} {names}")
    return "\n".join(lines)


def render_receipt_report(
    lines: list[ReceiptLine],
    total_cents: int,
    currency: str = "€",
    low_confidence: bool = False,
) -> str:
    header = "🧾 已入库（来自收据）"
    out = [header, ""]
    if low_confidence:
        out.append("⚠️ 识别置信度较低，请核对")
        out.append("")
    for ln in lines:
        icon = _CATEGORY_ICON.get(ln.category, "🥬")
        name = f"{ln.name} ({ln.original_name})" if ln.original_name else ln.name
        qty = f" ×{ln.quantity}" if ln.quantity > 1 else ""
        out.append(f"{icon} {name}{qty} · {_money(ln.unit_price_cents, currency)}")
    out.extend(["", f"合计 {_money(total_cents, currency)}"])
    return "\n".join(out)


def render_inventory(items: list[Item], today: date) -> str:
    header = "🧊 冰箱内容"
    footer = f"更新于 {today.isoformat()}"
    if not items:
        return f"{header}\n\n（空）\n\n{footer}"

    known = [it for it in items if it.expiry_date is not None]
    unknown = [it for it in items if it.expiry_date is None]

    lines = [header, ""]
    for it in sorted(known, key=lambda x: x.expiry_date):
        icon, label = _status(it.expiry_date - today)
        name = f"{it.name} ({it.original_name})" if it.original_name else it.name
        lines.append(f"{icon} {name} · {label}（{it.expiry_date.strftime('%m-%d')}）")
    for it in unknown:
        name = f"{it.name} ({it.original_name})" if it.original_name else it.name
        lines.append(f"⚪ {name} · 保质期未知")
    lines.extend(["", footer])
    return "\n".join(lines)


def _status(delta: timedelta) -> tuple[str, str]:
    days = delta.days
    if days < 0:
        return "🔴", f"已过期 {-days} 天，记得扔"
    if days == 0:
        return "🟠", "今天到期"
    if days <= 2:
        return "🟡", f"还剩 {days} 天"
    return "🟢", f"还剩 {days} 天"
