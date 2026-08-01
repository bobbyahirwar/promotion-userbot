from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from database import get_db
from handlers.admin import owner_only


# ── Helpers ───────────────────────────────────────────────────────────────────

async def get_blacklisted_ids() -> set[int]:
    """Return the set of blacklisted chat_ids (used by the promotion engine)."""
    db = get_db()
    docs = await db.blacklist.find({}, {"chat_id": 1}).to_list(None)
    return {d["chat_id"] for d in docs}


# ── Commands ──────────────────────────────────────────────────────────────────

@owner_only
async def cmd_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = get_db()
    entries = await db.blacklist.find().to_list(None)
    if not entries:
        await update.message.reply_text("📋 Blacklist is empty.")
        return
    lines = [
        f"{i}. {e.get('title', 'Unknown')} ({e['chat_id']})"
        for i, e in enumerate(entries, 1)
    ]
    await update.message.reply_text("🚫 Blacklisted Groups\n\n" + "\n".join(lines))


@owner_only
async def cmd_addblacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: /addblacklist <group_number_from_/groups | chat_id>"
        )
        return

    db = get_db()
    arg = context.args[0]

    try:
        value = int(arg)
    except ValueError:
        await update.message.reply_text("❌ Provide a valid number or chat ID.")
        return

    # If the number looks like a list index (positive, reasonably small) try
    # to resolve it against the stored groups list first.
    chat_id = None
    title = None
    if 1 <= value <= 100:
        groups = await db.groups.find().to_list(100)
        if value <= len(groups):
            chat_id = groups[value - 1]["chat_id"]
            title   = groups[value - 1].get("title", str(chat_id))

    # Otherwise treat the raw value as a chat_id.
    if chat_id is None:
        chat_id = value
        title   = str(chat_id)

    exists = await db.blacklist.find_one({"chat_id": chat_id})
    if exists:
        await update.message.reply_text(f"⚠️ {title} is already blacklisted.")
        return

    await db.blacklist.insert_one({"chat_id": chat_id, "title": title})
    await update.message.reply_text(f"✅ Added to blacklist: {title}")


@owner_only
async def cmd_removeblacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /removeblacklist <number_from_/blacklist>")
        return

    try:
        index = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Provide a valid number.")
        return

    db = get_db()
    entries = await db.blacklist.find().to_list(None)
    if index < 1 or index > len(entries):
        await update.message.reply_text(f"❌ Invalid number. Choose between 1 and {len(entries)}.")
        return

    entry = entries[index - 1]
    await db.blacklist.delete_one({"_id": entry["_id"]})
    await update.message.reply_text(
        f"✅ Removed from blacklist: {entry.get('title', entry['chat_id'])}"
    )


# ── Registration ──────────────────────────────────────────────────────────────

def register(app):
    app.add_handler(CommandHandler("blacklist",       cmd_blacklist))
    app.add_handler(CommandHandler("addblacklist",    cmd_addblacklist))
    app.add_handler(CommandHandler("removeblacklist", cmd_removeblacklist))
