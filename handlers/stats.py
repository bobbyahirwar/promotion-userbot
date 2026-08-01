from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from database import get_db
from handlers.admin import owner_only
from core.promotion import is_running


async def get_stat(db, key: str, default=0):
    doc = await db.stats.find_one({"key": key})
    return doc["value"] if doc else default


# ── Command ───────────────────────────────────────────────────────────────────

@owner_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = get_db()

    total_groups      = await db.groups.count_documents({})
    blacklisted       = await db.blacklist.count_documents({})
    active_groups     = max(total_groups - blacklisted, 0)
    promo_state       = "🟢 Running" if is_running() else "🔴 Stopped"
    total_sent        = await get_stat(db, "total_sent")
    total_failed      = await get_stat(db, "total_failed")
    last_promo_raw    = await get_stat(db, "last_promotion_time", default=None)
    last_promo        = str(last_promo_raw) if last_promo_raw else "Never"

    text = (
        "📊 Promotion Statistics\n\n"
        f"👥 Total Groups: {total_groups}\n"
        f"🚫 Blacklisted Groups: {blacklisted}\n"
        f"✅ Active Groups: {active_groups}\n"
        f"📣 Promotion: {promo_state}\n\n"
        f"📤 Total Messages Sent: {total_sent}\n"
        f"❌ Total Failed: {total_failed}\n"
        f"🕐 Last Promotion: {last_promo}"
    )
    await update.message.reply_text(text)


# ── Registration ──────────────────────────────────────────────────────────────

def register(app):
    app.add_handler(CommandHandler("stats", cmd_stats))
