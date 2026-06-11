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

from fridgebot.llm import ParsedInput, ParseQueue, parse_receipt_with_retry
from fridgebot.storage import Database

from .render import render_inventory, render_receipt_report

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_ID = int(os.environ["TELEGRAM_OWNER_ID"])
CHANNEL_ID: str | None = os.getenv("TELEGRAM_CHANNEL_ID") or None
DB_PATH = os.getenv("DB_PATH", "fridge.db")
DAILY_REFRESH_HOUR = int(os.getenv("DAILY_REFRESH_HOUR", "8"))
CURRENCY = os.getenv("CURRENCY", "€")

PENDING: dict[str, ParsedInput] = {}
ICONS = {"add": "➕", "consume": "➖", "update": "✏️"}


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
        "冰箱助手在线。发食材描述即可，比如：\n"
        "• 今天买了一颗生菜和一盒鸡蛋\n"
        "• 牛奶还能放 3 天\n"
        "• 酸奶过期了扔了\n"
        "• 冰箱里还有啥"
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
        expiry = date.fromisoformat(ln.expiry_date) if ln.expiry_date else today
        qty = max(1, ln.quantity)
        for _ in range(qty):
            await db.add_item(
                ln.name,
                today,
                expiry,
                original_name=ln.original_name,
                price_cents=ln.unit_price_cents,
                batch_id=batch_id,
                category=ln.category,
            )
        total += ln.unit_price_cents * qty
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

    # 撤销收据入库：不依赖内存 PENDING，靠 DB 里的 batch_id 持久撤销（进程重启仍可用）。
    if action == "undo":
        db: Database = context.application.bot_data["db"]
        n = await db.delete_batch(token)
        logger.info(f"undo batch {token}: removed {n} item(s)")
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
    for op in parsed.operations:
        icon = ICONS[op.intent]
        if op.intent == "add":
            entry = date.fromisoformat(op.entry_date) if op.entry_date else date.today()
            expiry = date.fromisoformat(op.expiry_date) if op.expiry_date else entry
            await db.add_item(op.item, entry, expiry, original_name=op.original_name)
            display = f"{op.item} ({op.original_name})" if op.original_name else op.item
            logger.info(f"add {display!r} entry={entry} expiry={expiry}")
            report.append(f"{icon} {display}")
        elif op.intent == "consume":
            ok = await db.consume_oldest(op.item)
            display = f"{op.item} ({op.original_name})" if op.original_name else op.item
            logger.info(f"consume {display!r} {'ok' if ok else 'not_found'}")
            report.append(f"{icon} {display}" if ok else f"⚠️ {display} 不在冰箱中")
        elif op.intent == "update":
            entry = date.fromisoformat(op.entry_date) if op.entry_date else None
            expiry = date.fromisoformat(op.expiry_date) if op.expiry_date else None
            ok = await db.update_oldest(op.item, entry=entry, expiry=expiry)
            display = f"{op.item} ({op.original_name})" if op.original_name else op.item
            logger.info(f"update {display!r} entry={entry} expiry={expiry} {'ok' if ok else 'not_found'}")
            report.append(f"{icon} {display}" if ok else f"⚠️ {display} 不在冰箱中")

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
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(private_text, on_message))
    app.add_handler(MessageHandler(private_photo, on_photo))
    app.add_error_handler(on_error)
    app.job_queue.run_daily(daily_refresh_job, time=time(hour=DAILY_REFRESH_HOUR))
    app.run_polling()


if __name__ == "__main__":
    main()
