import asyncio
import io
import random
from datetime import datetime, timezone

from pyrogram.errors import (
    FloodWait,
    SlowmodeWait,       # correct Pyrogram name (lowercase 'm')
    ChatWriteForbidden,
    UserBannedInChannel,
    PeerIdInvalid,
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

        messages = await db.messages.find().to_list(5)
        if not messages:
            print("⚠️  No messages saved — waiting 5 minutes.")
            await asyncio.sleep(300)
            continue

        groups = await db.groups.find().to_list(100)
        if not groups:
            print("⚠️  No groups stored — waiting 5 minutes.")
            await asyncio.sleep(300)
            continue

        # ── Blacklist filter ─────────────────────────────────────────────────
        from handlers.blacklist import get_blacklisted_ids
        blacklisted = await get_blacklisted_ids()
        active_groups = [g for g in groups if g["chat_id"] not in blacklisted]

        random.shuffle(messages)

        total_groups = len(active_groups)
        successful = 0
        failed = 0

        for i, group in enumerate(active_groups):
            if not _running:
                break

            chat_id = group["chat_id"]

            # Resolve display name
            try:
                chat = await userbot.get_chat(chat_id)
                name = chat.title or str(chat_id)
            except Exception:
                name = str(chat_id)

            print(f"📤 [{i + 1}/{total_groups}] Sending to {name}")

            group_ok = True
            try:
                for idx, msg in enumerate(messages):
                    await _send_one(bot, chat_id, msg)
                    await _inc_stat(db, "total_sent")
                    print(f"  ✅ Sent message {idx + 1}/{len(messages)}")
                    if idx < len(messages) - 1:
                        await asyncio.sleep(random.uniform(2, 3))

            except FloodWait as e:
                # Wait exactly as long as Telegram demands, then keep going
                print(f"  ⚠️  FloodWait {e.value}s on {name} — waiting, then continuing")
                await _inc_stat(db, "total_failed")
                group_ok = False
                await asyncio.sleep(e.value)
            except SlowmodeWait as e:
                wait_time = getattr(e, "value", 30)
                print(f"  ⚠️  SlowmodeWait {wait_time}s on {name} — skipping")
                await _inc_stat(db, "total_failed")
                group_ok = False
            except (ChatWriteForbidden, UserBannedInChannel, PeerIdInvalid) as e:
                print(f"  ⚠️  {type(e).__name__} on {name} — skipping")
                await _inc_stat(db, "total_failed")
                group_ok = False
            except Exception as e:
                print(f"  ⚠️  Network/unknown error on {name}: {e} — skipping")
                await _inc_stat(db, "total_failed")
                group_ok = False

            if group_ok:
                successful += 1
            else:
                failed += 1

            # Delay between groups: strictly 5–10s; skip after the last group
            if _running and i < total_groups - 1:
                await asyncio.sleep(random.uniform(5, 10))

        # ── End-of-cycle summary ─────────────────────────────────────────────
        await _set_stat(db, "last_promotion_time",
                        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))

        print(
            f"\n📊 Cycle Summary:\n"
            f"   Total Groups : {total_groups}\n"
            f"   Successful   : {successful}\n"
            f"   Failed       : {failed}\n"
        )

        # ── Exact 5-minute cadence: subtract time already spent ──────────────
        elapsed = asyncio.get_event_loop().time() - cycle_start
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
