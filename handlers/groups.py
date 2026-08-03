from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from pyrogram.enums import ChatType
from pyrogram.errors import (
    PeerIdInvalid,
    ChannelPrivate,
    UserNotParticipant,
    UsernameNotOccupied,
    UsernameInvalid,
)
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


# ── Startup validator ─────────────────────────────────────────────────────────

# Errors that mean the group is permanently inaccessible — remove it from DB.
_INVALID_PEER_ERRORS = (
    PeerIdInvalid,
    ChannelPrivate,
    UserNotParticipant,
    UsernameNotOccupied,
    UsernameInvalid,
)

async def validate_and_clean_groups() -> tuple[int, int]:
    """
    Called once at startup (after userbot connects and DB is ready).
    Checks every stored group with get_chat(); removes any that are
    permanently inaccessible.  Never raises — always returns normally.

    Returns (kept, removed).
    """
    db = get_db()
    groups = await db.groups.find({}, {"chat_id": 1}).to_list(None)

    kept = 0
    removed = 0

    for doc in groups:
        chat_id = doc["chat_id"]
        try:
            await userbot.get_chat(chat_id)
            kept += 1
        except _INVALID_PEER_ERRORS as e:
            await db.groups.delete_one({"chat_id": chat_id})
            print(f"   🗑️  Removed invalid group {chat_id}: {type(e).__name__}")
            removed += 1
        except ValueError:
            # Some IDs surface as plain ValueError before reaching Pyrogram
            await db.groups.delete_one({"chat_id": chat_id})
            print(f"   🗑️  Removed invalid group {chat_id}: ValueError")
            removed += 1
        except Exception:
            # Temporary error (FloodWait, network, etc.) — keep the group
            kept += 1

    return kept, removed


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
