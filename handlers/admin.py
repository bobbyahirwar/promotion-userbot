from functools import wraps

from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from config import OWNER_ID
from core.account_context import account_context

# ── Access guard ──────────────────────────────────────────────────────────────

def owner_only(handler):
    """Decorator: silently ignore commands from anyone who isn't OWNER_ID."""
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or update.effective_user.id != OWNER_ID:
            return
        if handler.__name__ in {
            "cmd_start",
            "cmd_help",
            "cmd_status",
            "cmd_startpromo",
            "cmd_stoppromo",
        }:
            await handler(update, context)
            return
        from core.accounts import account_manager

        account_id = account_manager.selected_account_id
        message = update.effective_message
        if account_id is None:
            if message:
                await message.reply_text("No account is selected. Use /start to manage accounts.")
            return
        client = account_manager.clients.get(account_id)
        if not client or not client.is_connected:
            if message:
                await message.reply_text(
                    "The selected account is disconnected. Use /start to reconnect or select another account."
                )
            return
        with account_context(account_id):
            await handler(update, context)
    return wrapper


# ── Command handlers ──────────────────────────────────────────────────────────

@owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from handlers.accounts import show_main_menu

    await show_main_menu(update.effective_message)


@owner_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📋 Available Commands\n\n"
        "🔧 General\n"
        "/start — Start the control panel\n"
        "/help — Show this help message\n"
        "Use Connected Accounts in /start to add, select, or remove accounts.\n"
        "/status — Show current system status\n"
        "/stats — Show promotion statistics\n"
        "/health — Show real-time bot health report\n\n"
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
        "/autorotate — Configure multi-group saved-message rotation\n"
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
    from core.accounts import account_manager
    from core.account_context import account_context
    from database import get_db

    account_id = account_manager.selected_account_id
    if account_id is None:
        accounts = await account_manager.list_accounts()
        await update.message.reply_text(
            f"Connected Accounts: {len(accounts)}\n"
            "Current Account: None\nUse /start to select or add an account."
        )
        return
    with account_context(account_id):
        db = get_db()
        msg_count = await db.messages.count_documents({})
        group_count = await db.groups.count_documents({})
        promo_line = "🟢 Running" if is_running(account_id) else "🔴 Stopped"
    client = account_manager.clients.get(account_id)
    session_line = "✅ Connected" if client and client.is_connected else "❌ Disconnected"

    text = (
        "🤖 Promotion Userbot Status\n\n"
        "✅ Bot Connected\n"
        f"User Session: {session_line}\n"
        "✅ MongoDB Connected\n\n"
        f"Current Account: {account_id}\n"
        f"Promotion: {promo_line}\n"
        f"Groups: {group_count}\n"
        f"Messages: {msg_count}"
    )
    await update.message.reply_text(text)


# ── Registration ──────────────────────────────────────────────────────────────

def register(app):
    """Register all admin handlers onto the given Application instance."""
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
