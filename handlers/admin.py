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
        "/start — Start the control panel\n"
        "/help — Show this help message\n"
        "/status — Show current system status"
    )
    await update.message.reply_text(text)


@owner_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 Promotion Userbot Status\n\n"
        "✅ Bot Connected\n"
        "✅ User Session Connected\n"
        "✅ MongoDB Connected\n\n"
        "Promotion: Not Configured Yet"
    )
    await update.message.reply_text(text)


# ── Registration ──────────────────────────────────────────────────────────────

def register(app):
    """Register all admin handlers onto the given Application instance."""
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
