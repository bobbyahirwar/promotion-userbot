from pyrogram.enums import ChatType
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

from database import get_db
from core.userbot import userbot
from handlers.admin import owner_only


async def sync_joined_groups() -> tuple[int, int, int, int]:
    """
    Iterate every userbot dialog and upsert groups/supergroups into MongoDB.

    For each group/supergroup:
      - If new → insert (added count goes up).
      - If already stored → update title and username (updated count goes up).
    Private chats, bots, and channels are silently skipped.

    Returns:
        (total_scanned, added, updated, skipped)
        total_scanned — total dialogs iterated
        added         — groups newly inserted into DB
        updated       — groups already in DB (title/username refreshed)
        skipped       — dialogs that are not a group or supergroup
    """
    db = get_db()

    total_scanned = 0
    added = 0
    updated = 0
    skipped = 0

    async for dialog in userbot.get_dialogs():
        total_scanned += 1
        chat = dialog.chat

        if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            skipped += 1
            continue

        result = await db.groups.update_one(
            {"chat_id": chat.id},
            {"$set": {
                "chat_id": chat.id,
                "title":    chat.title    or "",
                "username": chat.username or "",
            }},
            upsert=True,
        )

        if result.upserted_id is not None:
            added += 1
        else:
            updated += 1

    print(
        f"🔄 Sync complete — scanned: {total_scanned}, "
        f"added: {added}, updated: {updated}, skipped: {skipped}"
    )
    return total_scanned, added, updated, skipped


# ── Command handler ───────────────────────────────────────────────────────────

@owner_only
async def cmd_syncgroups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/syncgroups — Re-scan all joined groups and update the database."""
    await update.message.reply_text("🔄 Syncing joined groups, please wait…")

    try:
        total, added, updated, skipped = await sync_joined_groups()
    except Exception as e:
        await update.message.reply_text(f"❌ Sync failed: {e}")
        return

    text = (
        "✅ Group Sync Complete\n\n"
        f"📊 Total Scanned : {total}\n"
        f"➕ Added         : {added}\n"
        f"🔄 Updated       : {updated}\n"
        f"⏭️  Skipped       : {skipped}"
    )
    await update.message.reply_text(text)


# ── Registration ──────────────────────────────────────────────────────────────

def register(app):
    app.add_handler(CommandHandler("syncgroups", cmd_syncgroups))
