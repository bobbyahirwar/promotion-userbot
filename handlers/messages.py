from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from database import get_db
from handlers.admin import owner_only

MAX_MESSAGES = 5


# ── Helpers ───────────────────────────────────────────────────────────────────

def _preview(msg: dict) -> str:
    """Return a one-line human-readable description of a stored message."""
    if msg["type"] == "text":
        return f"📝 {msg['text'][:60]}"
    if msg["type"] == "photo":
        cap = msg.get("caption", "")
        return "🖼 Photo" + (f" — {cap[:50]}" if cap else "")
    if msg["type"] == "video":
        cap = msg.get("caption", "")
        return "🎥 Video" + (f" — {cap[:50]}" if cap else "")
    return "❓ Unknown"


# ── Commands ──────────────────────────────────────────────────────────────────

@owner_only
async def cmd_addmsg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    replied = update.message.reply_to_message
    if not replied:
        await update.message.reply_text("❌ Reply to a message to save it.")
        return

    db = get_db()
    count = await db.messages.count_documents({})
    if count >= MAX_MESSAGES:
        await update.message.reply_text("❌ Maximum 5 messages allowed.")
        return

    if replied.text:
        doc = {"type": "text", "text": replied.text}
    elif replied.photo:
        doc = {
            "type": "photo",
            "file_id": replied.photo[-1].file_id,
            "caption": replied.caption or "",
        }
    elif replied.video:
        doc = {
            "type": "video",
            "file_id": replied.video.file_id,
            "caption": replied.caption or "",
        }
    else:
        await update.message.reply_text("❌ Unsupported message type. Send text, photo, or video.")
        return

    await db.messages.insert_one(doc)
    await update.message.reply_text("✅ Message saved.")


@owner_only
async def cmd_listmsg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = get_db()
    msgs = await db.messages.find().to_list(MAX_MESSAGES)
    if not msgs:
        await update.message.reply_text("📭 No messages saved.")
        return

    lines = [f"{i}. {_preview(m)}" for i, m in enumerate(msgs, 1)]
    await update.message.reply_text("\n".join(lines))


@owner_only
async def cmd_delmsg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /delmsg <number>")
        return

    try:
        index = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Provide a valid number.")
        return

    db = get_db()
    msgs = await db.messages.find().to_list(MAX_MESSAGES)
    if index < 1 or index > len(msgs):
        await update.message.reply_text(f"❌ Invalid number. Choose between 1 and {len(msgs)}.")
        return

    await db.messages.delete_one({"_id": msgs[index - 1]["_id"]})
    await update.message.reply_text(f"✅ Message {index} deleted.")


@owner_only
async def cmd_clearmsg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = get_db()
    result = await db.messages.delete_many({})
    await update.message.reply_text(f"✅ Cleared {result.deleted_count} message(s).")


# ── Registration ──────────────────────────────────────────────────────────────

def register(app):
    """Register all message-manager handlers."""
    app.add_handler(CommandHandler("addmsg",   cmd_addmsg))
    app.add_handler(CommandHandler("listmsg",  cmd_listmsg))
    app.add_handler(CommandHandler("delmsg",   cmd_delmsg))
    app.add_handler(CommandHandler("clearmsg", cmd_clearmsg))
