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
# Temporary errors (FloodWait, SlowmodeWait, network) are NOT listed here.
_PERMANENT_ERRORS = (
    ChatWriteForbidden,
    UserBannedInChannel,
    PeerIdInvalid,
    ChatAdminRequired,
    ChannelPrivate,
    UserNotParticipant,
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


# ── Promotion loop ────────────────────────────────────────────────────────────

async def _loop(bot):
    global _running

    print("✅ Promotion Started")

    while _running:
        cycle_start = asyncio.get_event_loop().time()
        db = get_db()

        messages = await db.messages.find().to_list(100)
        if not messages:
            print("⚠️  No messages saved — waiting 5 minutes.")
            await asyncio.sleep(300)
            continue

        groups = await db.groups.find().to_list(100)
        if not groups:
            print("⚠️  No groups stored — waiting 5 minutes.")
            await asyncio.sleep(300)
            continue

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

        for i, group in enumerate(active_groups):
            if not _running:
                break

            chat_id = group["chat_id"]

            # Resolve display name — never let this block or crash the loop
            try:
                chat = await userbot.get_chat(chat_id)
                name = chat.title or str(chat_id)
            except Exception:
                name = str(chat_id)

            msgs_sent    = 0
            # "ok" | "skipped" | "failed"
            group_result = "ok"

            try:
                await _send_one(bot, chat_id, current_msg)
                await _inc_stat(db, "total_sent")
                msgs_sent = 1

            except FloodWait as e:
                # Temporary — wait exactly as long as Telegram requires, then
                # move on.  Never deactivate the group for this.
                print(f"  ⚠️  FloodWait {e.value}s — waiting then resuming")
                await _inc_stat(db, "total_failed")
                group_result = "failed"
                await asyncio.sleep(e.value)

            except SlowmodeWait as e:
                # Temporary — skip this cycle, retry next time.
                wait_time = getattr(e, "value", 30)
                print(f"  ⚠️  SlowmodeWait {wait_time}s — skipping group (temporary)")
                await _inc_stat(db, "total_failed")
                group_result = "skipped"

            except _PERMANENT_ERRORS as e:
                # Permanent permission error — deactivate immediately so the
                # group is excluded from every future cycle.
                reason = type(e).__name__
                await _inc_stat(db, "total_failed")
                await mark_inactive(chat_id, reason)
                group_result = "inactive"

            except ValueError as e:
                # pyrogram/utils.py:get_peer_type() raises a plain ValueError
                # (not a Pyrogram RPC class) for IDs outside all known ranges.
                # "Peer id invalid: <id>" is a permanent condition — deactivate.
                err_str = str(e).lower()
                if "peer id invalid" in err_str or "peer_id_invalid" in err_str:
                    await _inc_stat(db, "total_failed")
                    await mark_inactive(chat_id, f"PeerIdInvalid(ValueError): {e}")
                    group_result = "inactive"
                else:
                    print(f"  ⚠️  ValueError on {name}: {e} — continuing")
                    await _inc_stat(db, "total_failed")
                    group_result = "failed"

            except Exception as e:
                # Temporary (network, timeout, unknown) — do NOT deactivate.
                print(f"  ⚠️  Temporary error on {name}: {e} — continuing")
                await _inc_stat(db, "total_failed")
                group_result = "failed"

            # ── Per-group result line ────────────────────────────────────────
            if group_result == "ok":
                label = f"Sent (1 msg)"
                successful += 1
            elif group_result == "skipped":
                label = "Skipped (temporary)"
                skipped += 1
            elif group_result == "inactive":
                label = "Deactivated (permanent error)"
                deactivated += 1
            else:
                label = "Failed (temporary)"
                failed += 1

            print(f"[{i + 1}/{total_groups}] {name} -> {label}")

            # 3–5 s between groups; no delay after the very last group
            if _running and i < total_groups - 1:
                await asyncio.sleep(random.uniform(3, 5))

        # ── End-of-cycle summary ─────────────────────────────────────────────
        await _set_stat(db, "last_promotion_time",
                        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))

        # Advance the rotating index and persist it for the next cycle /
        # restart.  Wraps via modulo when fetched at the top of the next cycle.
        next_idx = (cycle_msg_idx + 1) % len(messages)
        await _set_stat(db, "current_message_index", next_idx)

        print(
            f"\n📊 Cycle complete (message #{cycle_msg_idx + 1}/{len(messages)}):\n"
            f"   Total Groups : {total_groups}\n"
            f"   Successful   : {successful}\n"
            f"   Skipped      : {skipped}\n"
            f"   Failed       : {failed}\n"
            f"   Deactivated  : {deactivated}\n"
            f"   Next message : #{next_idx + 1}\n"
        )

        # ── Exact 5-minute cadence: subtract time already spent ──────────────
        elapsed   = asyncio.get_event_loop().time() - cycle_start
        remaining = max(0.0, 300.0 - elapsed)
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
