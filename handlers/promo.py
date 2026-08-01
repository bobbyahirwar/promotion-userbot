from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from handlers.admin import owner_only
from core import promotion


# ── Commands ──────────────────────────────────────────────────────────────────

@owner_only
async def cmd_startpromo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if promotion.is_running():
        await update.message.reply_text("⚠️ Promotion is already running.")
        return
    await promotion.start_promotion(context.bot)
    await update.message.reply_text("✅ Promotion Started.")


@owner_only
async def cmd_stoppromo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not promotion.is_running():
        await update.message.reply_text("⚠️ Promotion is not running.")
        return
    await promotion.stop_promotion()
    await update.message.reply_text("🔴 Promotion Stopped.")


# ── Registration ──────────────────────────────────────────────────────────────

def register(app):
    app.add_handler(CommandHandler("startpromo", cmd_startpromo))
    app.add_handler(CommandHandler("stoppromo",  cmd_stoppromo))
