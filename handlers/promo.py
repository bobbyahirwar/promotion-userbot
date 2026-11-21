import logging

from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from handlers.admin import owner_only
from core import promotion
from core.accounts import account_manager
from database import get_db

_log = logging.getLogger(__name__)


# ── Commands ──────────────────────────────────────────────────────────────────

@owner_only
async def cmd_startpromo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    records = await account_manager.list_accounts()
    if not records:
        await update.message.reply_text("No Telegram accounts are configured.")
        return
    statuses = ["Promotion status by account:"]
    for record in records:
        account_id = int(record["account_id"])
        client = account_manager.clients.get(account_id)
        name = record.get("name") or record.get("first_name") or str(account_id)
        if not client or not client.is_connected:
            statuses.append(f"• {name} ({account_id}): Disconnected — not started")
            continue
        try:
            started = await promotion.start_promotion(context.bot, account_id)
        except Exception as exc:
            _log.exception("Could not start promotion for account %s.", account_id)
            statuses.append(
                f"• {name} ({account_id}): Failed to start ({type(exc).__name__})"
            )
        else:
            state = "Started" if started else "Already running"
            statuses.append(f"• {name} ({account_id}): {state}")
    await update.message.reply_text("\n".join(statuses))


@owner_only
async def cmd_stoppromo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    records = await account_manager.list_accounts()
    if not records:
        await update.message.reply_text("No Telegram accounts are configured.")
        return
    statuses = ["Promotion status by account:"]
    for record in records:
        account_id = int(record["account_id"])
        name = record.get("name") or record.get("first_name") or str(account_id)
        try:
            was_running = promotion.is_running(account_id)
            await promotion.stop_promotion(account_id)
        except Exception as exc:
            _log.exception("Could not stop promotion for account %s.", account_id)
            statuses.append(f"• {name} ({account_id}): Failed to stop ({type(exc).__name__})")
        else:
            state = "Stopped" if was_running else "Already stopped"
            statuses.append(f"• {name} ({account_id}): {state}")
    await update.message.reply_text("\n".join(statuses))


@owner_only
async def cmd_debugpromo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle debug mode on/off. State persists in MongoDB across restarts."""
    db = get_db()
    doc = await db.state.find_one({"key": "debug_mode"})
    current = bool(doc and doc.get("value"))
    new_val = not current

    await db.state.update_one(
        {"key": "debug_mode"},
        {"$set": {"value": new_val}},
        upsert=True,
    )

    if new_val:
        text = (
            "🟢 Debug Mode Enabled ✅\n\n"
            "You will now receive live promotion updates:\n"
            "• Cycle start notification\n"
            "• Per-group result with ID, status, error, time taken & running counters\n"
            "• FloodWait / SlowmodeWait alerts\n"
            "• Full end-of-cycle summary report\n\n"
            "🛡 Safety Status\n"
            "Failure Rate Protection: ACTIVE\n"
            "Threshold: 50%\n"
            "Status: MONITORING"
        )
    else:
        text = (
            "🔴 Debug Mode Disabled ❌\n\n"
            "Promotion will run silently.\n"
            "Reports and logs are still saved to MongoDB.\n"
            "Use /lastreport or /logs to check results.\n\n"
            "🛡 Safety Status\n"
            "Failure Rate Protection: ACTIVE\n"
            "Threshold: 50%\n"
            "Status: MONITORING"
        )

    await update.message.reply_text(text)


@owner_only
async def cmd_lastreport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the most recent promotion cycle report."""
    db = get_db()
    doc = await db.promotion_reports.find_one(sort=[("_id", -1)])
    if not doc:
        await update.message.reply_text("⚠️ No promotion reports found yet.")
        return

    ts = doc.get("timestamp")
    ts_str = ts.strftime("%Y-%m-%d %H:%M UTC") if ts else "Unknown"

    next_sec = doc.get("next_cycle_seconds", 0)
    next_min = next_sec // 60
    next_s   = next_sec % 60
    next_str = f"{next_min} Minutes {next_s} Seconds" if next_min > 0 else f"{next_s} Seconds"

    text = (
        "📊 Last Promotion Report\n\n"
        f"🕒 Time: {ts_str}\n"
        f"📨 Message: #{doc.get('message_num', '?')}/{doc.get('total_messages', '?')}\n\n"
        f"Total Groups: {doc.get('total_groups', 0)}\n"
        f"Successful: {doc.get('successful', 0)}\n"
        f"Failed: {doc.get('failed', 0)}\n"
        f"Skipped: {doc.get('skipped', 0)}\n"
        f"Deactivated: {doc.get('deactivated', 0)}\n\n"
        f"Next Cycle: {next_str}"
    )
    await update.message.reply_text(text)


@owner_only
async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display the last 30 promotion events."""
    db = get_db()
    events = await (
        db.promotion_logs.find()
        .sort("_id", -1)
        .limit(30)
        .to_list(30)
    )
    if not events:
        await update.message.reply_text("⚠️ No promotion logs found yet.")
        return

    # Reverse so oldest-first (chronological order)
    events = list(reversed(events))

    lines = ["📋 Last 30 Promotion Events\n"]
    for ev in events:
        ts = ev.get("timestamp")
        ts_str = ts.strftime("%H:%M:%S") if ts else "?"
        etype = ev.get("type", "")

        if etype == "cycle_start":
            lines.append(
                f"[{ts_str}] 🚀 Cycle Started — "
                f"Msg #{ev.get('message_num', '?')}, "
                f"{ev.get('total_groups', 0)} groups"
            )
        elif etype == "cycle_end":
            lines.append(
                f"[{ts_str}] 📊 Cycle Done — "
                f"✅{ev.get('successful', 0)} "
                f"❌{ev.get('failed', 0)} "
                f"⏭{ev.get('skipped', 0)} "
                f"🚫{ev.get('deactivated', 0)}"
            )
        elif etype == "group_result":
            result = ev.get("result", "")
            name   = ev.get("group_name", str(ev.get("chat_id", "?")))
            idx    = ev.get("index", "?")
            total  = ev.get("total", "?")
            error  = ev.get("error", "")

            if result == "ok":
                icon = "✅"
            elif result == "skipped":
                icon = "⏭"
            elif result == "inactive":
                icon = "🚫"
            else:
                icon = "❌"

            entry = f"[{ts_str}] {icon} [{idx}/{total}] {name}"
            if error:
                entry += f"\n        ↳ {error}"
            lines.append(entry)
        elif etype == "flood_wait":
            wait_s = ev.get("telegram_wait_seconds", ev.get("flood_wait_seconds", "?"))
            tot_s = ev.get("cooldown_seconds", "?")
            grp_info = ""
            if ev.get("group_index") and ev.get("total_groups"):
                grp_info = f" | Paused at {ev.get('group_index')}/{ev.get('total_groups')}"
            lines.append(
                f"[{ts_str}] 🚨 REAL TELEGRAM FLOODWAIT — "
                f"Wait: {wait_s}s (Cooldown: {tot_s}s){grp_info}"
            )
        elif etype == "error_pause":
            lines.append(
                f"[{ts_str}] ⚠️ OTHER SEND ERROR PAUSE — "
                f"{ev.get('reason', 'Temporary error')} ({ev.get('cooldown_seconds', 0)}s)"
            )
        else:
            lines.append(f"[{ts_str}] {etype}")

    text = "\n".join(lines)

    # Telegram message limit is 4096 chars — truncate from the top if needed
    if len(text) > 4000:
        text = "…(truncated)\n" + text[-4000:]

    await update.message.reply_text(text)


# ── Registration ──────────────────────────────────────────────────────────────

def register(app):
    app.add_handler(CommandHandler("startpromo",  cmd_startpromo))
    app.add_handler(CommandHandler("stoppromo",   cmd_stoppromo))
    app.add_handler(CommandHandler("debugpromo",  cmd_debugpromo))
    app.add_handler(CommandHandler("lastreport",  cmd_lastreport))
    app.add_handler(CommandHandler("logs",        cmd_logs))
