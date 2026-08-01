from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

from database import get_db
from handlers.admin import owner_only


# ── DB helpers (used by the promotion loop and command handlers) ───────────────

async def get_inactive_ids() -> set:
    """Return the set of chat_ids currently marked inactive."""
    db = get_db()
    docs = await db.inactive_groups.find({}, {"chat_id": 1}).to_list(None)
    return {d["chat_id"] for d in docs}


async def mark_inactive(chat_id: int, reason: str) -> None:
    """Upsert a group as inactive, storing the error reason and timestamp."""
    db = get_db()
    await db.inactive_groups.update_one(
        {"chat_id": chat_id},
        {"$set": {
            "chat_id": chat_id,
            "reason": reason,
            "disabled_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        }},
        upsert=True,
    )
    print(f"  🚫 Group {chat_id} marked inactive: {reason}")


async def _reactivate_one(chat_id: int) -> bool:
    """Delete a single group from the inactive collection. Returns True if found."""
    db = get_db()
    result = await db.inactive_groups.delete_one({"chat_id": chat_id})
    return result.deleted_count > 0


async def _reactivate_all() -> int:
    """Delete all inactive groups. Returns the number removed."""
    db = get_db()
    result = await db.inactive_groups.delete_many({})
    return result.deleted_count


# ── Command handlers ──────────────────────────────────────────────────────────

@owner_only
async def cmd_inactivegroups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List every group that has been auto-deactivated."""
    db = get_db()
    docs = await db.inactive_groups.find().to_list(None)

    if not docs:
        await update.message.reply_text("✅ No inactive groups.")
        return

    lines = [f"🚫 Inactive Groups ({len(docs)} total)\n"]
    for i, doc in enumerate(docs, 1):
        lines.append(
            f"{i}. ID: <code>{doc['chat_id']}</code>\n"
            f"   Reason: {doc.get('reason', 'Unknown')}\n"
            f"   Since: {doc.get('disabled_at', 'Unknown')}"
        )

    await update.message.reply_text("\n\n".join(lines), parse_mode="HTML")


@owner_only
async def cmd_reactivategroups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reactivate ALL inactive groups at once."""
    count = await _reactivate_all()
    if count == 0:
        await update.message.reply_text("ℹ️ No inactive groups to reactivate.")
    else:
        await update.message.reply_text(f"✅ Reactivated {count} group(s). They will rejoin the next cycle.")


@owner_only
async def cmd_reactivate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reactivate a single group by its ID: /reactivate <group_id>"""
    if not context.args:
        await update.message.reply_text("Usage: /reactivate <group_id>")
        return

    try:
        chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid group ID — must be a number.")
        return

    found = await _reactivate_one(chat_id)
    if found:
        await update.message.reply_text(f"✅ Group <code>{chat_id}</code> reactivated.", parse_mode="HTML")
    else:
        await update.message.reply_text(f"ℹ️ Group <code>{chat_id}</code> was not in the inactive list.", parse_mode="HTML")


# ── Registration ──────────────────────────────────────────────────────────────

def register(app):
    app.add_handler(CommandHandler("inactivegroups",   cmd_inactivegroups))
    app.add_handler(CommandHandler("reactivategroups", cmd_reactivategroups))
    app.add_handler(CommandHandler("reactivate",       cmd_reactivate))
