import asyncio
import io
import random
from datetime import datetime, timezone

from pyrogram.errors import (
    FloodWait,
    SlowmodeWait,           # correct Pyrogram name (lowercase 'm')
    ChatWriteForbidden,
    UserBannedInChannel,
    PeerIdInvalid,
    ChatAdminRequired,
    ChannelPrivate,
    UserNotParticipant,
)

# Permanent permission errors that should deactivate a group.
# Temporary errors (FloodWait, SlowmodeWait, ChatWriteForbidden, network)
# are NOT listed here — they are retried on the next cycle.
_PERMANENT_ERRORS = (
    UserBannedInChannel,
    PeerIdInvalid,
    ChatAdminRequired,
    ChannelPrivate,
    UserNotParticipant,
)

from config import PROMOTION_INTERVAL_SECONDS, OWNER_ID
from database import get_db
from core.userbot import userbot

_task: asyncio.Task = None
_running: bool = False

# ── Helpers ───────────────────────────────────────────────────────────────────

async def _download(bot, file_id: str) -> io.BytesIO:
    """Download a Bot-API file into memory and return a seeked BytesIO buffer."""
    f = await bot.get_file(file_id)
    buf = io.BytesIO()
    await f.download_to_memory(buf)
    buf.seek(0)
    return buf


async def _send_one(bot, chat_id: int, msg: dict):
    """Send a single stored message to chat_id via the userbot."""
    caption = msg.get("caption") or None
    if msg["type"] == "text":
        await userbot.send_message(chat_id, msg["text"])
    elif msg["type"] == "photo":
        buf = await _download(bot, msg["file_id"])
        await userbot.send_photo(chat_id, buf, caption=caption)
    elif msg["type"] == "video":
        buf = await _download(bot, msg["file_id"])
        await userbot.send_video(chat_id, buf, caption=caption)


async def _inc_stat(db, key: str, amount: int = 1):
    """Atomically increment a stats counter."""
    await db.stats.update_one(
        {"key": key},
        {"$inc": {"value": amount}},
        upsert=True,
    )


async def _set_stat(db, key: str, value):
    """Set a stats value."""
    await db.stats.update_one(
        {"key": key},
        {"$set": {"value": value}},
        upsert=True,
    )


async def _notify(bot, text: str):
    """Send a Telegram message to the owner. Never raises — log failures only."""
    try:
        await bot.send_message(OWNER_ID, text)
    except Exception as e:
        print(f"⚠️  Owner notify failed: {e}")


async def _log_event(db, event: dict):
    """
    Insert one event into promotion_logs and trim to the last 200 entries.
    Never raises — errors are printed only.
    """
    try:
        event.setdefault("timestamp", datetime.now(timezone.utc))
        await db.promotion_logs.insert_one(event)
        # Keep only the most recent 200 log entries
        count = await db.promotion_logs.count_documents({})
        if count > 200:
            oldest_cursor = (
                db.promotion_logs.find({}, {"_id": 1})
                .sort("_id", 1)
                .limit(count - 200)
            )
            oldest = await oldest_cursor.to_list(count - 200)
            ids = [d["_id"] for d in oldest]
            await db.promotion_logs.delete_many({"_id": {"$in": ids}})
    except Exception as e:
        print(f"⚠️  Log event failed: {e}")


async def _is_debug_mode(db) -> bool:
    """Return True if debug mode is currently enabled in MongoDB."""
    try:
        doc = await db.state.find_one({"key": "debug_mode"})
        return bool(doc and doc.get("value"))
    except Exception:
        return False


# ── Promotion loop ────────────────────────────────────────────────────────────

async def _loop(bot):
    global _running

    print("✅ Promotion Started")

    while _running:
        cycle_start = asyncio.get_event_loop().time()
        db = get_db()

        messages = await db.messages.find().to_list(100)
        if not messages:
            print(f"⚠️  No messages saved — waiting {PROMOTION_INTERVAL_SECONDS}s.")
            await asyncio.sleep(PROMOTION_INTERVAL_SECONDS)
            continue

        groups = await db.groups.find().to_list(100)
        if not groups:
            print(f"⚠️  No groups stored — waiting {PROMOTION_INTERVAL_SECONDS}s.")
            await asyncio.sleep(PROMOTION_INTERVAL_SECONDS)
            continue

        # ── Check debug mode once per cycle ──────────────────────────────────
        debug = await _is_debug_mode(db)

        # ── Rotating message selection ────────────────────────────────────────
        # Load persisted index, pick one message for this entire cycle, then
        # advance the pointer so the next cycle uses the following message.
        idx_doc = await db.state.find_one({"key": "current_message_index"})
        raw_idx = idx_doc["value"] if idx_doc and "value" in idx_doc else 0
        cycle_msg_idx = int(raw_idx) % len(messages)
        current_msg   = messages[cycle_msg_idx]
        print(f"📨 Cycle message: #{cycle_msg_idx + 1}/{len(messages)}")

        # ── Blacklist + inactive filter ──────────────────────────────────────
        from handlers.blacklist import get_blacklisted_ids
        from handlers.inactive_groups import get_inactive_ids, mark_inactive

        blacklisted = await get_blacklisted_ids()
        inactive    = await get_inactive_ids()
        excluded    = blacklisted | inactive
        active_groups = [g for g in groups if g["chat_id"] not in excluded]

        total_groups = len(active_groups)
        successful = 0
        skipped    = 0
        failed     = 0
        deactivated = 0

        # ── Cycle-start notification (debug only) ─────────────────────────────
        now_str = datetime.now(timezone.utc).strftime("%I:%M %p")
        if debug:
            cycle_start_text = (
                "🚀 Promotion Cycle Started\n\n"
                f"🕒 Time: {now_str} UTC\n"
                f"📨 Message: #{cycle_msg_idx + 1}/{len(messages)}\n"
                f"👥 Total Groups: {total_groups}\n\n"
                "━━━━━━━━━━━━━━"
            )
            await _notify(bot, cycle_start_text)

        await _log_event(db, {
            "type": "cycle_start",
            "message_num": cycle_msg_idx + 1,
            "total_messages": len(messages),
            "total_groups": total_groups,
        })

        for i, group in enumerate(active_groups):
            if not _running:
                break

            chat_id = group["chat_id"]
            group_timer_start = asyncio.get_event_loop().time()

            # Resolve display name — never let this block or crash the loop
            try:
                chat = await userbot.get_chat(chat_id)
                name = chat.title or str(chat_id)
            except Exception:
                name = str(chat_id)

            # "ok" | "skipped" | "failed" | "inactive"
            group_result = "ok"
            error_detail = ""

            try:
                await _send_one(bot, chat_id, current_msg)
                await _inc_stat(db, "total_sent")

            except FloodWait as e:
                # Temporary — wait exactly as long as Telegram requires, then
                # move on.  Never deactivate the group for this.
                error_detail = f"FloodWait {e.value} seconds"
                print(f"  ⚠️  FloodWait {e.value}s — waiting then resuming")
                await _inc_stat(db, "total_failed")
                group_result = "failed"
                # Notify about the wait before sleeping so the owner sees it immediately
                if debug:
                    await _notify(bot, f"⏳ FloodWait: waiting {e.value}s before continuing...")
                await asyncio.sleep(e.value)

            except SlowmodeWait as e:
                # Temporary — skip this cycle, retry next time.
                wait_time = getattr(e, "value", 30)
                error_detail = f"Slowmode {wait_time} seconds"
                print(f"  ⚠️  SlowmodeWait {wait_time}s — skipping group (temporary)")
                await _inc_stat(db, "total_failed")
                group_result = "skipped"

            except _PERMANENT_ERRORS as e:
                # Permanent permission error — deactivate immediately so the
                # group is excluded from every future cycle.
                error_detail = type(e).__name__
                await _inc_stat(db, "total_failed")
                await mark_inactive(chat_id, error_detail)
                group_result = "inactive"

            except ValueError as e:
                # pyrogram/utils.py:get_peer_type() raises a plain ValueError
                # (not a Pyrogram RPC class) for IDs outside all known ranges.
                # "Peer id invalid: <id>" is a permanent condition — deactivate.
                err_str = str(e).lower()
                if "peer id invalid" in err_str or "peer_id_invalid" in err_str:
                    error_detail = f"PeerIdInvalid: {e}"
                    await _inc_stat(db, "total_failed")
                    await mark_inactive(chat_id, f"PeerIdInvalid(ValueError): {e}")
                    group_result = "inactive"
                else:
                    error_detail = f"ValueError: {e}"
                    print(f"  ⚠️  ValueError on {name}: {e} — continuing")
                    await _inc_stat(db, "total_failed")
                    group_result = "failed"

            except Exception as e:
                # Temporary (network, timeout, unknown) — do NOT deactivate.
                error_detail = f"{type(e).__name__}: {e}"
                print(f"  ⚠️  Temporary error on {name}: {e} — continuing")
                await _inc_stat(db, "total_failed")
                group_result = "failed"

            # ── Per-group result ─────────────────────────────────────────────
            time_taken = asyncio.get_event_loop().time() - group_timer_start

            if group_result == "ok":
                status_emoji = "✅ Sent"
                label        = "Sent (1 msg)"
                successful  += 1
            elif group_result == "skipped":
                status_emoji = "⏭ Skipped"
                label        = "Skipped (temporary)"
                skipped     += 1
            elif group_result == "inactive":
                status_emoji = "🚫 Deactivated"
                label        = "Deactivated (permanent error)"
                deactivated += 1
            else:
                status_emoji = "❌ Failed"
                label        = "Failed (temporary)"
                failed      += 1

            remaining_groups = total_groups - (i + 1)
            print(f"[{i + 1}/{total_groups}] {name} -> {label}")

            # ── Per-group debug notification ──────────────────────────────────
            if debug:
                lines = [
                    f"[{i + 1}/{total_groups}]",
                    f"Group: {name}",
                    f"ID: {chat_id}",
                    f"Result: {status_emoji}",
                ]
                if error_detail:
                    lines.append(f"Error: {error_detail}")
                lines.append(f"Time: {time_taken:.2f}s")
                lines.append("")
                lines.append(
                    f"✅ {successful} | ❌ {failed} | "
                    f"⏭ {skipped} | 🚫 {deactivated} | "
                    f"⏳ {remaining_groups} remaining"
                )
                await _notify(bot, "\n".join(lines))

            # Save to logs (always, regardless of debug mode)
            await _log_event(db, {
                "type": "group_result",
                "index": i + 1,
                "total": total_groups,
                "group_name": name,
                "chat_id": chat_id,
                "result": group_result,
                "error": error_detail,
                "time_taken": round(time_taken, 2),
            })

            # 5–10 s between groups; no delay after the very last group
            if _running and i < total_groups - 1:
                await asyncio.sleep(random.uniform(5, 10))

        # ── End-of-cycle summary ─────────────────────────────────────────────
        await _set_stat(db, "last_promotion_time",
                        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))

        # Advance the rotating index and persist it for the next cycle /
        # restart.  Wraps via modulo when fetched at the top of the next cycle.
        next_idx = (cycle_msg_idx + 1) % len(messages)
        await _set_stat(db, "current_message_index", next_idx)

        # ── Exact 5-minute cadence: subtract time already spent ──────────────
        elapsed   = asyncio.get_event_loop().time() - cycle_start
        remaining = max(0.0, 300.0 - elapsed)

        next_cycle_min = int(remaining // 60)
        next_cycle_sec = int(remaining % 60)
        if next_cycle_min > 0:
            next_cycle_str = f"{next_cycle_min} Minutes {next_cycle_sec} Seconds"
        else:
            next_cycle_str = f"{next_cycle_sec} Seconds"

        print(
            f"\n📊 Cycle complete (message #{cycle_msg_idx + 1}/{len(messages)}):\n"
            f"   Total Groups : {total_groups}\n"
            f"   Successful   : {successful}\n"
            f"   Skipped      : {skipped}\n"
            f"   Failed       : {failed}\n"
            f"   Deactivated  : {deactivated}\n"
            f"   Next message : #{next_idx + 1}\n"
        )

        # ── End-of-cycle Telegram report (debug only) ─────────────────────────
        if debug:
            report_text = (
                "📊 Promotion Report\n\n"
                f"Total Groups: {total_groups}\n"
                f"Successful: {successful}\n"
                f"Failed: {failed}\n"
                f"Skipped: {skipped}\n"
                f"Deactivated: {deactivated}\n\n"
                f"Next Cycle: {next_cycle_str}"
            )
            await _notify(bot, report_text)

        # ── Save cycle report to MongoDB (always) ─────────────────────────────
        report_doc = {
            "timestamp": datetime.now(timezone.utc),
            "message_num": cycle_msg_idx + 1,
            "total_messages": len(messages),
            "total_groups": total_groups,
            "successful": successful,
            "failed": failed,
            "skipped": skipped,
            "deactivated": deactivated,
            "next_cycle_seconds": int(remaining),
        }
        try:
            await db.promotion_reports.insert_one(report_doc)
        except Exception as e:
            print(f"⚠️  Failed to save cycle report: {e}")

        await _log_event(db, {
            "type": "cycle_end",
            "message_num": cycle_msg_idx + 1,
            "total_groups": total_groups,
            "successful": successful,
            "failed": failed,
            "skipped": skipped,
            "deactivated": deactivated,
        })

        if _running and remaining > 0:
            print(f"⏳ Next cycle in {remaining:.0f}s.")
            await asyncio.sleep(remaining)

    print("🔴 Promotion Stopped")


# ── Public API ────────────────────────────────────────────────────────────────

async def start_promotion(bot):
    global _running, _task
    if _running:
        return
    _running = True
    db = get_db()
    await db.state.update_one(
        {"key": "promotion_running"},
        {"$set": {"value": True}},
        upsert=True,
    )
    _task = asyncio.create_task(_loop(bot))


async def stop_promotion():
    global _running, _task
    _running = False
    db = get_db()
    await db.state.update_one(
        {"key": "promotion_running"},
        {"$set": {"value": False}},
        upsert=True,
    )
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None


def is_running() -> bool:
    return _running


async def restore_state(bot):
    """Resume the promotion loop automatically if it was running before restart."""
    db = get_db()
    doc = await db.state.find_one({"key": "promotion_running"})
    if doc and doc.get("value"):
        print("🔄 Resuming promotion from previous session...")
        await start_promotion(bot)
