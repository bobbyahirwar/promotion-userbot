from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from config import OWNER_ID

# ── Access guard ──────────────────────────────────────────────────────────────

def owner_only(handler):
    """Decorator: silently ignore commands from anyone who isn't OWNER_ID."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user and update.effective_user.id == OWNER_ID:
            await handler(update, context)
    return wrapper


# ── Command handlers ──────────────────────────────────────────────────────────

@owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Promotion Control Panel Ready.")


@owner_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📋 Available Commands\n\n"
        "🔧 General\n"
        "/start — Start the control panel\n"
        "/help — Show this help message\n"
        "/status — Show current system status\n"
        "/stats — Show promotion statistics\n\n"
        "✉️ Messages\n"
        "/addmsg — Reply to a message to save it\n"
        "/listmsg — List all saved messages\n"
        "/delmsg <number> — Delete a saved message\n"
        "/clearmsg — Delete all saved messages\n\n"
        "👥 Groups\n"
        "/groups — Scan and store joined groups\n"
        "/syncgroups — Sync joined groups (upsert, keeps existing data)\n\n"
        "🚫 Blacklist\n"
        "/blacklist — Show blacklisted groups\n"
        "/addblacklist <number|id> — Add a group to the blacklist\n"
        "/removeblacklist <number> — Remove a group from the blacklist\n\n"
        "📣 Promotion\n"
        "/startpromo — Start automatic promotion\n"
        "/stoppromo — Stop automatic promotion\n"
        "/debugpromo — Toggle live debug updates (on/off)\n"
        "/lastreport — Show the last promotion cycle report\n"
        "/logs — Show last 30 promotion events\n\n"
        "🚫 Inactive Groups\n"
        "/inactivegroups — Show all auto-deactivated groups with reason\n"
        "/reactivategroups — Reactivate all inactive groups\n"
        "/reactivate <group_id> — Reactivate one group by ID\n"
        "/resetinactive — Clear all inactive groups and restore them"
    )
    await update.message.reply_text(text)


@owner_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from core.promotion import is_running
    from database import get_db

    db = get_db()
    msg_count   = await db.messages.count_documents({})
    group_count = await db.groups.count_documents({})
    promo_line  = "🟢 Running" if is_running() else "🔴 Stopped"

    text = (
        "🤖 Promotion Userbot Status\n\n"
        "✅ Bot Connected\n"
        "✅ User Session Connected\n"
        "✅ MongoDB Connected\n\n"
        f"Promotion: {promo_line}\n"
        f"Groups: {group_count}\n"
        f"Messages: {msg_count}"
    )
    await update.message.reply_text(text)


# ── Registration ──────────────────────────────────────────────────────────────

def register(app):
    """Register all admin handlers onto the given Application instance."""
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
