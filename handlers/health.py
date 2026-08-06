import os
from datetime import datetime, timezone

import psutil
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

from database import get_db
from handlers.admin import owner_only
from core.userbot import userbot
from core.promotion import is_running
from core import uptime


async def _get_stat(db, key: str, default=0):
    """Read a single value from the stats collection."""
    doc = await db.stats.find_one({"key": key})
    return doc["value"] if doc else default


# ── Command ───────────────────────────────────────────────────────────────────

@owner_only
async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display a real-time health report. Never crashes — each check is isolated."""

    lines = ["🏥 Bot Health Report\n"]

    # ── Bot status ────────────────────────────────────────────────────────────
    # If this command is executing, the bot is reachable.
    lines.append("✅ Bot Status: Running")

    # ── Userbot (Pyrogram) status ─────────────────────────────────────────────
    try:
        if userbot.is_connected:
            me = await userbot.get_me()
            lines.append(f"✅ Userbot: Connected (@{me.username or me.id})")
        else:
            lines.append("❌ Userbot: Disconnected")
    except Exception as e:
        lines.append(f"❌ Userbot: Error — {e}")

    # ── MongoDB status ────────────────────────────────────────────────────────
    db = get_db()
    mongo_ok = False
    try:
        if db is None:
            raise RuntimeError("Database not initialised")
        await db.client.admin.command("ping")
        mongo_ok = True
        lines.append("✅ MongoDB: Connected")
    except Exception as e:
        lines.append(f"❌ MongoDB: Disconnected — {e}")

    # ── Promotion status ──────────────────────────────────────────────────────
    promo_state = "🟢 Running" if is_running() else "🔴 Stopped"
    lines.append(f"🔄 Promotion: {promo_state}")

    lines.append("")  # blank separator

    # ── Group counts ──────────────────────────────────────────────────────────
    if mongo_ok:
        try:
            total_groups    = await db.groups.count_documents({})
            inactive_count  = await db.inactive_groups.count_documents({})
            blacklisted     = await db.blacklist.count_documents({})
            active_count    = max(total_groups - inactive_count - blacklisted, 0)

            lines.append(f"👥 Total Groups: {total_groups}")
            lines.append(f"✅ Active Groups: {active_count}")
            lines.append(f"🚫 Inactive Groups: {inactive_count}")
        except Exception as e:
            lines.append(f"👥 Groups: Error — {e}")
    else:
        lines.append("👥 Groups: N/A (MongoDB unavailable)")

    # ── Saved messages ────────────────────────────────────────────────────────
    if mongo_ok:
        try:
            msg_count = await db.messages.count_documents({})
            lines.append(f"📨 Saved Promotion Messages: {msg_count}")
        except Exception as e:
            lines.append(f"📨 Saved Messages: Error — {e}")
    else:
        lines.append("📨 Saved Messages: N/A")

    lines.append("")  # blank separator

    # ── Promotion statistics ──────────────────────────────────────────────────
    if mongo_ok:
        try:
            total_sent   = await _get_stat(db, "total_sent",   default=0)
            total_failed = await _get_stat(db, "total_failed", default=0)
            last_promo   = await _get_stat(db, "last_promotion_time", default=None)

            lines.append(f"📊 Total Messages Sent: {total_sent}")
            lines.append(f"❌ Total Failed: {total_failed}")
            lines.append(f"⏭ Total Skipped: N/A (tracked per-cycle only)")
            lines.append(f"🕒 Last Promotion: {last_promo or 'Never'}")
        except Exception as e:
            lines.append(f"📊 Stats: Error — {e}")
    else:
        lines.append("📊 Stats: N/A (MongoDB unavailable)")

    lines.append("")  # blank separator

    # ── System metrics ────────────────────────────────────────────────────────
    lines.append(f"⏱ Uptime: {uptime.get_uptime_str()}")

    try:
        proc = psutil.Process(os.getpid())
        mem_mb = proc.memory_info().rss / (1024 * 1024)
        lines.append(f"💾 Memory Usage: {mem_mb:.1f} MB")
    except Exception as e:
        lines.append(f"💾 Memory Usage: Error — {e}")

    try:
        cpu_pct = psutil.cpu_percent(interval=0.2)
        lines.append(f"🖥 CPU Usage: {cpu_pct:.1f}%")
    except Exception as e:
        lines.append(f"🖥 CPU Usage: Error — {e}")

    # ── Database size ─────────────────────────────────────────────────────────
    if mongo_ok:
        try:
            db_stats  = await db.command("dbStats")
            db_size_mb = db_stats.get("dataSize", 0) / (1024 * 1024)
            lines.append(f"📦 Database Size: {db_size_mb:.2f} MB")
        except Exception as e:
            lines.append(f"📦 Database Size: Unavailable — {e}")
    else:
        lines.append("📦 Database Size: N/A")

    # ── Server time ───────────────────────────────────────────────────────────
    server_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines.append(f"📅 Server Time: {server_time}")

    await update.message.reply_text("\n".join(lines))


# ── Registration ──────────────────────────────────────────────────────────────

def register(app):
    app.add_handler(CommandHandler("health", cmd_health))
