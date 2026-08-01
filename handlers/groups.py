from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from pyrogram.enums import ChatType
from database import get_db
from handlers.admin import owner_only
from core.userbot import userbot

MAX_GROUPS = 100


# ── Scanner ───────────────────────────────────────────────────────────────────

async def scan_and_store() -> tuple[int, int]:
    """
    Iterate userbot dialogs, collect group IDs (groups + supergroups only),
    store up to MAX_GROUPS in the DB, and return (total_found, stored_count).
    """
    db = get_db()

    group_ids = []
    async for dialog in userbot.get_dialogs():
        chat = dialog.chat
        if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            group_ids.append(chat.id)

    total_found = len(group_ids)
    to_store = group_ids[:MAX_GROUPS]

    await db.groups.delete_many({})
    if to_store:
        await db.groups.insert_many([{"chat_id": gid} for gid in to_store])

    return total_found, len(to_store)


# ── Command ───────────────────────────────────────────────────────────────────

@owner_only
async def cmd_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Scanning groups, please wait...")

    try:
        total, usable = await scan_and_store()
    except Exception as e:
        await update.message.reply_text(f"❌ Group scan failed: {e}")
        return

    text = f"👥 Groups\n\nTotal Groups: {total}\nUsable Groups: {usable}"
    if total > MAX_GROUPS:
        text += f"\n\n⚠️ Only the first {MAX_GROUPS} groups were stored."

    await update.message.reply_text(text)


# ── Registration ──────────────────────────────────────────────────────────────

def register(app):
    """Register all group-scanner handlers."""
    app.add_handler(CommandHandler("groups", cmd_groups))
