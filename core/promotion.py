import asyncio
import io
import random
from datetime import datetime, timedelta, timezone

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
    ChatWriteForbidden,
    UserBannedInChannel,
    PeerIdInvalid,
    ChatAdminRequired,
    ChannelPrivate,
    UserNotParticipant,
)

from config import (
    PROMOTION_INTERVAL_SECONDS,
    PROMOTION_MIN_DELAY_SECONDS,
    PROMOTION_MAX_DELAY_SECONDS,
    PROMOTION_COOLDOWN_SECONDS,
    PROMOTION_MAX_CONSECUTIVE_ERRORS,
    PROMOTION_MAX_FAILURE_RATE,
    PROMOTION_MIN_GROUPS_FOR_FAILURE_RATE,
    OWNER_ID,
)
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


def _normalize_datetime(dt) -> datetime | None:
    """Safely convert naive or aware datetime/ISO string into timezone-aware UTC datetime."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    if isinstance(dt, (int, float)):
        try:
            return datetime.fromtimestamp(dt, tz=timezone.utc)
        except Exception:
            return None
    if isinstance(dt, str):
        try:
            parsed = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            return None
    return None


async def _get_consecutive_error_count(db) -> int:
    """Return the persisted consecutive temporary-error counter."""
    doc = await db.state.find_one({"key": "promotion_consecutive_errors"})
    if not doc or doc.get("value") is None:
        return 0
    try:
        return int(doc["value"])
    except (TypeError, ValueError):
        return 0


async def _set_consecutive_error_count(db, value: int):
    """Persist the consecutive temporary-error count for future cycles."""
    await db.state.update_one(
        {"key": "promotion_consecutive_errors"},
        {"$set": {"value": max(0, int(value))}},
        upsert=True,
    )


async def _get_cooldown_state(db):
    """Return the active cooldown document if the promotion is paused."""
    doc = await db.state.find_one({"key": "promotion_cooldown_until"})
    if not doc or not doc.get("value"):
        return None
    until = _normalize_datetime(doc.get("value"))
    if not until:
        return None
    return {"until": until, "reason": await db.state.find_one({"key": "promotion_cooldown_reason"})}


async def _has_active_cooldown(db) -> bool:
    """True when the promo loop is globally paused by a FloodWait cooldown."""
    cooldown = await _get_cooldown_state(db)
    if not cooldown or not cooldown.get("until"):
        return False
    now = datetime.now(timezone.utc)
    return now < cooldown["until"]


async def _get_cooldown_remaining_seconds(db) -> float:
    """Return remaining cooldown seconds; 0 when no active cooldown exists."""
    cooldown = await _get_cooldown_state(db)
    if not cooldown or not cooldown.get("until"):
        return 0.0
    now = datetime.now(timezone.utc)
    remaining = (cooldown["until"] - now).total_seconds()
    return max(0.0, remaining)


async def _clear_cooldown(db):
    """Clear FloodWait cooldown state from MongoDB when the pause has ended."""
    await db.state.delete_many({
        "key": {
            "$in": [
                "promotion_cooldown_until",
                "promotion_cooldown_reason",
                "promotion_cooldown_started_at",
            ]
        }
    })


async def _is_safety_pause_active(db) -> bool:
    """True when a cycle-level safety pause is active in MongoDB."""
    doc = await db.state.find_one({"key": "promotion_safety_paused"})
    return bool(doc and doc.get("value"))


async def _trigger_safety_pause(db, cycle_summary: dict, bot=None):
    """Persist a cycle-level automatic safety pause when failures are too high."""
    now = datetime.now(timezone.utc)
    await db.state.update_one(
        {"key": "promotion_safety_paused"},
        {"$set": {"value": True, "updated_at": now}},
        upsert=True,
    )
    await db.state.update_one(
        {"key": "promotion_safety_pause_reason"},
        {"$set": {"value": "Cycle failure rate exceeded threshold", "updated_at": now}},
        upsert=True,
    )
    await db.state.update_one(
        {"key": "promotion_safety_pause_snapshot"},
        {"$set": {"value": cycle_summary, "updated_at": now}},
        upsert=True,
    )
    await db.state.update_one(
        {"key": "promotion_safety_pause_started_at"},
        {"$set": {"value": now, "updated_at": now}},
        upsert=True,
    )

    await _log_event(db, {
        "type": "safety_pause",
        "reason": "Cycle failure rate exceeded threshold",
        "total_groups": cycle_summary.get("total_groups"),
        "successful": cycle_summary.get("successful"),
        "failed": cycle_summary.get("failed"),
        "skipped": cycle_summary.get("skipped"),
        "deactivated": cycle_summary.get("deactivated"),
        "failure_rate": cycle_summary.get("failure_rate"),
        "timestamp": now,
    })

    if bot and await _is_debug_mode(db):
        await _notify(
            bot,
            "🛑 Promotion Safety Pause\n\n"
            "Cycle completed with unusually high failures.\n\n"
            f"👥 Groups: {cycle_summary.get('total_groups', 0)}\n"
            f"✅ Successful: {cycle_summary.get('successful', 0)}\n"
            f"❌ Failed: {cycle_summary.get('failed', 0)}\n"
            f"⏭ Skipped: {cycle_summary.get('skipped', 0)}\n"
            f"🚫 Deactivated: {cycle_summary.get('deactivated', 0)}\n\n"
            f"📉 Failure Rate: {cycle_summary.get('failure_rate_display', '0%')}\n\n"
            "Promotion has been automatically paused for safety.",
        )

    print(
        f"🛑 Promotion safety pause activated: failure_rate={cycle_summary.get('failure_rate_display', '0%')} "
        f"groups={cycle_summary.get('total_groups', 0)}"
    )


async def _clear_safety_pause(db):
    """Clear cycle safety pause state from MongoDB when the operator resets it."""
    await db.state.delete_many({
        "key": {
            "$in": [
                "promotion_safety_paused",
                "promotion_safety_pause_reason",
                "promotion_safety_pause_snapshot",
                "promotion_safety_pause_started_at",
            ]
        }
    })


async def _trigger_cooldown(
    db,
    reason: str,
    bot=None,
    flood_wait_seconds: int | None = None,
    consecutive_errors: int | None = None,
):
    """Persist the existing global cooldown and stop the current promotion cycle."""
    now = datetime.now(timezone.utc)
    cooldown_until = now + timedelta(seconds=PROMOTION_COOLDOWN_SECONDS)
    await db.state.update_one(
        {"key": "promotion_cooldown_until"},
        {"$set": {"value": cooldown_until, "updated_at": now}},
        upsert=True,
    )
    await db.state.update_one(
        {"key": "promotion_cooldown_reason"},
        {"$set": {"value": reason, "updated_at": now}},
        upsert=True,
    )
    await db.state.update_one(
        {"key": "promotion_cooldown_started_at"},
        {"$set": {"value": now, "updated_at": now}},
        upsert=True,
    )

    payload = {
        "type": "promotion_cooldown",
        "reason": reason,
        "cooldown_seconds": PROMOTION_COOLDOWN_SECONDS,
        "cooldown_until": cooldown_until,
        "started_at": now,
    }
    if flood_wait_seconds is not None:
        payload["flood_wait_seconds"] = int(flood_wait_seconds)
    if consecutive_errors is not None:
        payload["consecutive_errors"] = int(consecutive_errors)
    await _log_event(db, payload)

    if bot and await _is_debug_mode(db):
        if reason == "FloodWait":
            await _notify(
                bot,
                "🚨 PROMOTION COOLDOWN\n\n"
                "Reason: Telegram FloodWait\n"
                f"Telegram Wait: {int(flood_wait_seconds or 0)} seconds\n"
                f"Safety Cooldown: {PROMOTION_COOLDOWN_SECONDS} seconds\n\n"
                "Promotion has been paused.",
            )
        else:
            await _notify(
                bot,
                "🚨 PROMOTION AUTO-PAUSED\n\n"
                f"Reason: {reason}\n"
                f"Consecutive Errors: {int(consecutive_errors or 0)}\n"
                f"Cooldown: {PROMOTION_COOLDOWN_SECONDS} seconds\n\n"
                "Promotion has been paused.",
            )

    if reason == "FloodWait":
        print(
            f"🚨 FloodWait cooldown activated: {int(flood_wait_seconds or 0)}s wait, "
            f"{PROMOTION_COOLDOWN_SECONDS}s pause until {cooldown_until.isoformat()}"
        )
    else:
        print(
            f"🚨 Promotion auto-paused: {reason}, "
            f"consecutive errors={int(consecutive_errors or 0)}, "
            f"cooldown until {cooldown_until.isoformat()}"
        )


# ── Promotion loop ────────────────────────────────────────────────────────────

async def _loop(bot):
    global _running

    print("✅ Promotion Started")

    while _running:
        cycle_start = asyncio.get_event_loop().time()
        db = get_db()

        # FloodWait cooldown is global and must block any new promotion sends
        # until the pause expires, even after a process restart.
        if await _has_active_cooldown(db):
            remaining = await _get_cooldown_remaining_seconds(db)
            print(f"⏳ Promotion cooldown active — waiting {remaining:.0f}s before resuming")
            await asyncio.sleep(min(remaining, 60.0))
            continue

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
        consecutive_error_count = await _get_consecutive_error_count(db)

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
        cooldown_triggered = False

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
                # FloodWait is not ignored: Telegram imposes the wait and the bot
                # must stop the current cycle and enter a global cooldown.
                wait_seconds = int(getattr(e, "value", 0) or 0)
                error_detail = f"FloodWait {wait_seconds} seconds"
                print(f"  ⚠️  FloodWait {wait_seconds}s — pausing promotion for cooldown")
                await _inc_stat(db, "total_failed")
                group_result = "failed"
                await _trigger_cooldown(
                    db,
                    reason="FloodWait",
                    bot=bot,
                    flood_wait_seconds=wait_seconds,
                )
                cooldown_triggered = True
                break

            except SlowmodeWait as e:
                # Temporary — count it toward the consecutive-error safety check,
                # but do not deactivate the group or retry it in the same cycle.
                wait_time = getattr(e, "value", 30)
                error_detail = f"Slowmode {wait_time} seconds"
                print(f"  ⚠️  SlowmodeWait {wait_time}s — counting as temporary failure")
                await _inc_stat(db, "total_failed")
                group_result = "failed"
                consecutive_error_count += 1
                await _set_consecutive_error_count(db, consecutive_error_count)
                if consecutive_error_count >= PROMOTION_MAX_CONSECUTIVE_ERRORS:
                    await _trigger_cooldown(
                        db,
                        reason="Repeated temporary promotion errors",
                        bot=bot,
                        consecutive_errors=consecutive_error_count,
                    )
                    cooldown_triggered = True
                    break

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
                    print(f"  ⚠️  ValueError on {name}: {e} — counting as temporary failure")
                    await _inc_stat(db, "total_failed")
                    group_result = "failed"
                    consecutive_error_count += 1
                    await _set_consecutive_error_count(db, consecutive_error_count)
                    if consecutive_error_count >= PROMOTION_MAX_CONSECUTIVE_ERRORS:
                        await _trigger_cooldown(
                            db,
                            reason="Repeated temporary promotion errors",
                            bot=bot,
                            consecutive_errors=consecutive_error_count,
                        )
                        cooldown_triggered = True
                        break

            except Exception as e:
                # Temporary (network, timeout, unknown) — do NOT deactivate.
                error_detail = f"{type(e).__name__}: {e}"
                print(f"  ⚠️  Temporary error on {name}: {e} — counting as temporary failure")
                await _inc_stat(db, "total_failed")
                group_result = "failed"
                consecutive_error_count += 1
                await _set_consecutive_error_count(db, consecutive_error_count)
                if consecutive_error_count >= PROMOTION_MAX_CONSECUTIVE_ERRORS:
                    await _trigger_cooldown(
                        db,
                        reason="Repeated temporary promotion errors",
                        bot=bot,
                        consecutive_errors=consecutive_error_count,
                    )
                    cooldown_triggered = True
                    break

            # ── Per-group result ─────────────────────────────────────────────
            time_taken = asyncio.get_event_loop().time() - group_timer_start

            if group_result == "ok":
                status_emoji = "✅ Sent"
                label        = "Sent (1 msg)"
                successful  += 1
                await _set_consecutive_error_count(db, 0)
                consecutive_error_count = 0
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

            # Conservative inter-group rate limit for promotions only.
            # This delay is intentionally placed only between actual promotion
            # sends, not between owner/debug notifications. It does not affect
            # the 5-minute cycle cadence logic at the end of the cycle.
            if _running and i < total_groups - 1:
                wait_seconds = random.uniform(
                    PROMOTION_MIN_DELAY_SECONDS,
                    PROMOTION_MAX_DELAY_SECONDS,
                )
                print(
                    f"⏳ Inter-group rate limit: {wait_seconds:.1f}s before next promotion"
                )
                await asyncio.sleep(wait_seconds)

        if cooldown_triggered:
            print("⏳ FloodWait cooldown active — current cycle stopped before remaining groups")
            continue

        # ── End-of-cycle summary ─────────────────────────────────────────────
        await _set_stat(db, "last_promotion_time",
                        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))

        # Safety circuit-breaker: only evaluate the cycle failure rate against
        # groups actually attempted in this cycle, not against already-excluded
        # groups that were filtered out before the loop started.
        failure_rate = (failed + skipped + deactivated) / total_groups if total_groups else 0.0
        cycle_failure_threshold = PROMOTION_MAX_FAILURE_RATE
        should_pause_for_failure_rate = (
            total_groups >= PROMOTION_MIN_GROUPS_FOR_FAILURE_RATE
            and total_groups > 0
            and failure_rate >= cycle_failure_threshold
        )

        if should_pause_for_failure_rate:
            cycle_summary = {
                "timestamp": datetime.now(timezone.utc),
                "total_groups": total_groups,
                "successful": successful,
                "failed": failed,
                "skipped": skipped,
                "deactivated": deactivated,
                "failure_rate": failure_rate,
                "failure_rate_display": f"{failure_rate * 100:.0f}%",
                "reason": "Cycle failure rate exceeded threshold",
            }
            await _trigger_safety_pause(db, cycle_summary, bot)
            await _set_stat(db, "promotion_safety_last_cycle_failure_rate", failure_rate)
            await _set_stat(db, "promotion_safety_last_cycle_total_groups", total_groups)
            # The cycle is intentionally paused and must not continue sending. The
            # loop will remain stopped until the operator intervenes or the system is
            # reset; the existing cooldown logic is not used here to avoid altering
            # the established safety layering.
            _running = False
            await db.state.update_one(
                {"key": "promotion_running"},
                {"$set": {"value": False}},
                upsert=True,
            )
            break

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

    _running = False
    print("🔴 Promotion Stopped")


# ── Public API ────────────────────────────────────────────────────────────────

async def start_promotion(bot):
    global _running, _task
    if _running and _task is not None and not _task.done():
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
    global _running, _task
    return bool(_running and _task is not None and not _task.done())


async def restore_state(bot):
    """Resume the promotion loop automatically if it was running before restart."""
    db = get_db()
    doc = await db.state.find_one({"key": "promotion_running"})
    if doc and doc.get("value"):
        if await _has_active_cooldown(db) or await _is_safety_pause_active(db):
            print("⏳ Active safety pause detected after restart — promotion remains paused")
            await start_promotion(bot)
            return
        print("🔄 Resuming promotion from previous session...")
        await start_promotion(bot)
