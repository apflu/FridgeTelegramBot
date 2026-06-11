import os
import uuid
from datetime import date, time
from functools import wraps

from dotenv import load_dotenv
from loguru import logger

from fridgebot import logging_config as logconfig

load_dotenv()
logconfig.setup(level=os.getenv("LOG_LEVEL", "INFO"))

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from fridgebot.llm import (
    ParsedInput,
    ParseQueue,
    estimate_expiry_with_retry,
    parse_receipt_with_retry,
)
from fridgebot.storage import Database, MealItem

from .render import (
    render_expiry_reminder,
    render_inventory,
    render_receipt_report,
    render_stats,
)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_ID = int(os.environ["TELEGRAM_OWNER_ID"])
CHANNEL_ID: str | None = os.getenv("TELEGRAM_CHANNEL_ID") or None
DB_PATH = os.getenv("DB_PATH", "fridge.db")
DAILY_REFRESH_HOUR = int(os.getenv("DAILY_REFRESH_HOUR", "8"))
CURRENCY = os.getenv("CURRENCY", "€")
REMINDERS_KEY = "reminders_enabled"  # kv 键；缺省视为开启（按 DB 存，天然每用户独立）

PENDING: dict[str, ParsedInput] = {}
ICONS = {"add": "➕", "update": "✏️", "eat": "🍽", "finish": "🍽", "discard": "🗑"}


def owner_only(handler):
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else None
        if uid != OWNER_ID:
            logger.warning(f"rejected update from user_id={uid}")
            return
        return await handler(update, context)
    return wrapper


def _channel_id() -> str | int | None:
    if CHANNEL_ID is None:
        return None
    return int(CHANNEL_ID) if CHANNEL_ID.lstrip("-").isdigit() else CHANNEL_ID


@owner_only
async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "冰箱助手在线。\n"
        "📝 发食材描述，比如：\n"
        "• 今天买了一颗生菜和一盒鸡蛋\n"
        "• 今天吃了牛腱和土豆（记一餐，不移除）\n"
        "• 牛奶喝完了（记一餐 + 移除）\n"
        "• 酸奶过期扔了（移除，不算吃）\n"
        "• 冰箱里还有啥\n"
        "🧾 拍超市收据照片 → 自动记录食材与价格（保质期留空）\n"
        "⏳ /estimate → 给保质期未知的食材批量估算保质期\n"
        "📊 /stats → 总开销 / 餐数 / 平均每餐\n"
        "⏰ /due → 立即查看今天到期或已过期的食材\n"
        "🔕 /mute、🔔 /unmute → 关闭 / 开启每日到期提醒"
    )


@owner_only
async def on_estimate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.application.bot_data["db"]
    queue: ParseQueue = context.application.bot_data["queue"]

    items = await db.list_unknown_expiry()
    if not items:
        await update.message.reply_text("✅ 没有保质期未知的食材")
        return

    notice = await update.message.reply_text(f"⏳ 正在估算 {len(items)} 项保质期…")
    entries = [(it.name, it.original_name, it.entry_date) for it in items]
    try:
        result = await queue.submit_job(
            lambda: estimate_expiry_with_retry(entries, date.today()),
            label=f"estimate {len(items)} item(s)",
        )
    except Exception as e:
        logger.exception("estimate failed")
        await notice.edit_text(f"❌ 估算失败：{e}")
        return

    updated = 0
    for g in result.guesses:
        if 0 <= g.index < len(items) and g.expiry_date:
            try:
                await db.set_expiry(items[g.index].id, date.fromisoformat(g.expiry_date))
                updated += 1
            except ValueError:
                logger.warning(f"bad expiry_date from llm: {g.expiry_date!r}")

    logger.info(f"estimate: updated {updated}/{len(items)} item(s)")
    await notice.edit_text(f"✅ 已估算并更新 {updated} 项保质期")
    await refresh_channel(context)


@owner_only
async def on_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.application.bot_data["db"]
    spend = await db.spend_cents()
    meal_cost = await db.meal_cost_cents()
    meals = await db.count_meals()
    recent = await db.recent_meals(limit=5)
    await update.message.reply_text(render_stats(spend, meals, meal_cost, CURRENCY, recent))


@owner_only
async def on_due(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.application.bot_data["db"]
    today = date.today()
    due = await db.items_due_by(today)
    if not due:
        await update.message.reply_text("✅ 没有今天到期或已过期的食材")
        return
    await update.message.reply_text(render_expiry_reminder(due, today))


@owner_only
async def on_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.application.bot_data["db"]
    await set_reminders(db, False)
    await update.message.reply_text("🔕 已关闭每日到期提醒，发 /unmute 可重新开启")


@owner_only
async def on_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.application.bot_data["db"]
    await set_reminders(db, True)
    await update.message.reply_text("🔔 已开启每日到期提醒")


@owner_only
async def on_cleardebug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """debug：清空购买流水 + 餐食记录（总开销与餐数归零）。冰箱库存不动。"""
    db: Database = context.application.bot_data["db"]
    p = await db.clear_purchases()
    m = await db.clear_meals()
    logger.info(f"debug: cleared {p} purchase row(s) + {m} meal(s)")
    await update.message.reply_text(
        f"🧹 已清空：购买流水 {p} 条、餐食记录 {m} 餐，总开销与餐数归零。\n"
        f"冰箱库存（含发票录入的食材与单价）保留不动。"
    )


@owner_only
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    logger.info(f"msg: {text!r}")
    db: Database = context.application.bot_data["db"]
    queue: ParseQueue = context.application.bot_data["queue"]

    items = await db.list_items()
    existing = sorted({
        f"{i.name} ({i.original_name})" if i.original_name else i.name
        for i in items
    })

    try:
        parsed = await queue.submit(text, existing_items=existing)
    except Exception as e:
        logger.exception("parse failed")
        await update.message.reply_text(f"❌ 解析失败：{e}")
        return

    if parsed.confidence < 0.7 or parsed.kind == "unknown":
        await update.message.reply_text(
            f"🤔 没听懂：{parsed.reasoning}\n换个说法试试？"
        )
        return

    if parsed.kind == "query":
        await update.message.reply_text(render_inventory(items, date.today()))
        return

    summary = ["📝 识别以下操作："]
    for op in parsed.operations:
        icon = ICONS[op.intent]
        display = f"{op.item} ({op.original_name})" if op.original_name else op.item
        parts = [f"{icon} {display}"]
        if op.intent == "add" and op.expiry_date:
            parts.append(f"到期 {op.expiry_date}")
        elif op.intent == "update":
            if op.expiry_date:
                parts.append(f"到期 → {op.expiry_date}")
            if op.entry_date:
                parts.append(f"入库 → {op.entry_date}")
        elif op.intent == "eat":
            parts.append("吃了·留库存")
        elif op.intent == "finish":
            parts.append("吃完·移除")
        elif op.intent == "discard":
            parts.append("移除")
        summary.append("  " + " · ".join(parts))

    token = uuid.uuid4().hex[:12]
    PENDING[token] = parsed
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 确认", callback_data=f"y:{token}"),
        InlineKeyboardButton("❌ 取消", callback_data=f"n:{token}"),
    ]])
    await update.message.reply_text("\n".join(summary), reply_markup=keyboard)


@owner_only
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.application.bot_data["db"]
    queue: ParseQueue = context.application.bot_data["queue"]

    photo = update.message.photo[-1]  # 最大尺寸
    tg_file = await photo.get_file()
    image_bytes = bytes(await tg_file.download_as_bytearray())
    logger.info(f"receipt photo: {len(image_bytes)} bytes")
    notice = await update.message.reply_text("📷 识别收据中…")

    try:
        receipt = await queue.submit_job(
            lambda: parse_receipt_with_retry(image_bytes, date.today()),
            label="receipt",
        )
    except Exception as e:
        logger.exception("receipt parse failed")
        await notice.edit_text(f"❌ 识别失败：{e}")
        return

    logger.info(f"← receipt: {len(receipt.lines)} line(s) conf={receipt.confidence:.2f}")
    logger.info(f"  reasoning: {receipt.reasoning}")
    if not receipt.lines:
        await notice.edit_text(f"🤔 没识别到生鲜/速冻食材\n{receipt.reasoning}")
        return

    batch_id = uuid.uuid4().hex[:12]
    today = date.today()
    total = 0
    for ln in receipt.lines:
        qty = max(1, ln.quantity)
        for _ in range(qty):
            # 发票流不推断保质期：expiry 留空，待用户运行 /estimate 再批量估算。
            await db.add_item(
                ln.name,
                today,
                None,
                original_name=ln.original_name,
                price_cents=ln.unit_price_cents,
                batch_id=batch_id,
                category=ln.category,
            )
        line_total = ln.unit_price_cents * qty
        total += line_total
        # 记一笔购买流水（不可变；/stats 现算，撤销本单时按 batch 删除）
        await db.add_purchase(today, ln.name, line_total, batch_id)
        display = f"{ln.name} ({ln.original_name})" if ln.original_name else ln.name
        logger.info(f"add {display!r} ×{qty} price={ln.unit_price_cents} batch={batch_id}")

    text = render_receipt_report(
        receipt.lines, total, CURRENCY, low_confidence=receipt.confidence < 0.6
    )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("↩️ 撤销本单", callback_data=f"undo:{batch_id}")]]
    )
    await notice.edit_text(text, reply_markup=keyboard)
    await refresh_channel(context)


@owner_only
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action, _, token = q.data.partition(":")

    # 关闭到期提醒：内联按钮，写 DB 开关（持久），移除按钮并提示如何重开。
    if action == "mute":
        db: Database = context.application.bot_data["db"]
        await set_reminders(db, False)
        logger.info("reminders muted via button")
        await q.edit_message_text(f"{q.message.text}\n\n🔕 已关闭到期提醒（/unmute 重开）")
        return

    # 撤销收据入库：不依赖内存 PENDING，靠 DB 里的 batch_id 持久撤销（进程重启仍可用）。
    if action == "undo":
        db: Database = context.application.bot_data["db"]
        n = await db.delete_batch(token)
        await db.delete_purchases(token)  # 同步移除该单的购买流水
        logger.info(f"undo batch {token}: removed {n} item(s) + purchases")
        await q.edit_message_text(f"↩️ 已撤销本单，移除 {n} 项")
        await refresh_channel(context)
        return

    parsed = PENDING.pop(token, None)
    if parsed is None:
        await q.edit_message_text("⌛ 确认已失效，请重新输入")
        return
    if action == "n":
        logger.info(f"cancelled {len(parsed.operations)} op(s)")
        await q.edit_message_text("❌ 已取消")
        return

    db: Database = context.application.bot_data["db"]
    report: list[str] = []
    meal_items: list[MealItem] = []  # eat + finish → 记入这顿饭
    for op in parsed.operations:
        icon = ICONS[op.intent]
        display = f"{op.item} ({op.original_name})" if op.original_name else op.item
        if op.intent == "add":
            entry = date.fromisoformat(op.entry_date) if op.entry_date else date.today()
            # 文字流由 LLM 推断保质期；万一缺失则留空（未知），而不是误判为当天过期。
            expiry = date.fromisoformat(op.expiry_date) if op.expiry_date else None
            await db.add_item(op.item, entry, expiry, original_name=op.original_name)
            logger.info(f"add {display!r} entry={entry} expiry={expiry}")
            report.append(f"{icon} {display}")
        elif op.intent == "update":
            entry = date.fromisoformat(op.entry_date) if op.entry_date else None
            expiry = date.fromisoformat(op.expiry_date) if op.expiry_date else None
            ok = await db.update_oldest(op.item, entry=entry, expiry=expiry)
            logger.info(f"update {display!r} entry={entry} expiry={expiry} {'ok' if ok else 'not_found'}")
            report.append(f"{icon} {display}" if ok else f"⚠️ {display} 不在冰箱中")
        elif op.intent in ("eat", "finish"):
            # 价格快照：从库存中该项的最旧一条取单价（可能为 None），为未来每顿饭成本留底。
            existing = await db.oldest_item(op.item)
            meal_items.append(
                MealItem(
                    name=op.item,
                    original_name=op.original_name,
                    price_cents=existing.price_cents if existing else None,
                )
            )
            if op.intent == "finish":
                ok = await db.consume_oldest(op.item)
                logger.info(f"finish {display!r} {'removed' if ok else 'not_found'}")
                report.append(f"{icon} {display} 吃完" if ok else f"🍽 {display} 吃完（冰箱中无此项）")
            else:
                logger.info(f"eat {display!r} (kept)")
                report.append(f"{icon} {display} 吃了")
        elif op.intent == "discard":
            ok = await db.consume_oldest(op.item)
            logger.info(f"discard {display!r} {'removed' if ok else 'not_found'}")
            report.append(f"{icon} {display} 移除" if ok else f"⚠️ {display} 不在冰箱中")

    if meal_items:
        await db.add_meal(date.today(), meal_items)
        logger.info(f"meal recorded: {[m.name for m in meal_items]}")
        report.append(f"📒 已记录一餐（{len(meal_items)} 项）")

    await q.edit_message_text("✅ 已应用：\n" + "\n".join(report))
    await refresh_channel(context)


async def refresh_channel(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _channel_id()
    if chat_id is None:
        return
    db: Database = context.application.bot_data["db"]
    items = await db.list_items()
    text = render_inventory(items, date.today())

    msg_id = await db.get_kv("inventory_message_id")
    if msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=int(msg_id), text=text
            )
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
            logger.warning(f"edit failed, will send new: {e}")

    sent = await context.bot.send_message(chat_id=chat_id, text=text)
    await db.set_kv("inventory_message_id", str(sent.message_id))


async def daily_refresh_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("running daily refresh")
    try:
        await refresh_channel(context)
    except Exception:
        logger.exception("daily refresh failed")


def reminder_targets(context: ContextTypes.DEFAULT_TYPE) -> list[tuple[int | str, Database]]:
    """到期提醒的收件人 (chat_id, db) 列表。
    多用户扩展点：未来改为遍历每用户的 (uid, db)（每用户独立 DB 的注册表）。
    现在只有 owner + 单一全局 db。"""
    db: Database = context.application.bot_data["db"]
    return [(OWNER_ID, db)]


async def reminders_enabled(db: Database) -> bool:
    return (await db.get_kv(REMINDERS_KEY)) != "0"  # 缺省（无记录）= 开启


async def set_reminders(db: Database, enabled: bool) -> None:
    await db.set_kv(REMINDERS_KEY, "1" if enabled else "0")


async def send_expiry_reminder(bot, db: Database, chat_id: int | str) -> bool:
    """有"今天到期或已过期"的项才发一条提醒；提醒被关闭或无到期项则不发。返回是否发送。"""
    if not await reminders_enabled(db):
        return False
    today = date.today()
    due = await db.items_due_by(today)
    if not due:
        return False
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔕 关闭到期提醒", callback_data="mute")]]
    )
    await bot.send_message(
        chat_id=chat_id, text=render_expiry_reminder(due, today), reply_markup=keyboard
    )
    return True


async def expiry_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("running expiry reminder")
    for chat_id, db in reminder_targets(context):
        try:
            if await send_expiry_reminder(context.bot, db, chat_id):
                logger.info(f"expiry reminder sent to {chat_id}")
        except Exception:
            logger.exception(f"expiry reminder failed for {chat_id}")


async def post_init(app: Application):
    db = Database(DB_PATH)
    await db.connect()
    queue = ParseQueue(min_interval=13)
    await queue.start()
    app.bot_data["db"] = db
    app.bot_data["queue"] = queue
    logger.info(f"bot ready · owner={OWNER_ID} channel={CHANNEL_ID or '(disabled)'} db={DB_PATH}")


async def post_shutdown(app: Application):
    queue: ParseQueue = app.bot_data.get("queue")
    db: Database = app.bot_data.get("db")
    if queue:
        await queue.stop()
    if db:
        await db.close()


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.opt(exception=context.error).error("unhandled handler error")


def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    private_text = (
        filters.TEXT
        & ~filters.COMMAND
        & filters.ChatType.PRIVATE
        & filters.UpdateType.MESSAGE
    )
    private_photo = (
        filters.PHOTO
        & filters.ChatType.PRIVATE
        & filters.UpdateType.MESSAGE
    )
    app.add_handler(CommandHandler("start", on_start, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("estimate", on_estimate, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("stats", on_stats, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("due", on_due, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("mute", on_mute, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("unmute", on_unmute, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("cleardebug", on_cleardebug, filters=filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(private_text, on_message))
    app.add_handler(MessageHandler(private_photo, on_photo))
    app.add_error_handler(on_error)
    app.job_queue.run_daily(daily_refresh_job, time=time(hour=DAILY_REFRESH_HOUR))
    app.job_queue.run_daily(expiry_reminder_job, time=time(hour=DAILY_REFRESH_HOUR))
    app.run_polling()


if __name__ == "__main__":
    main()
