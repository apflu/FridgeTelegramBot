from datetime import date, timedelta

from db import Item


def render_inventory(items: list[Item], today: date) -> str:
    header = "🧊 冰箱内容"
    footer = f"更新于 {today.isoformat()}"
    if not items:
        return f"{header}\n\n（空）\n\n{footer}"

    lines = [header, ""]
    for it in sorted(items, key=lambda x: x.expiry_date):
        icon, label = _status(it.expiry_date - today)
        name = f"{it.name} ({it.original_name})" if it.original_name else it.name
        lines.append(f"{icon} {name} · {label}（{it.expiry_date.strftime('%m-%d')}）")
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
