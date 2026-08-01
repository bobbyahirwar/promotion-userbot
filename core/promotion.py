import asyncio
import io
import random
from datetime import datetime, timezone

from pyrogram.errors import (
    FloodWait,
    SlowModeWait,
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

        # ── Safe-sending cache (cleared each cycle) ──────────────────────────
        sent_this_cycle: set[tuple[int, str]] = set()

        random.shuffle(messages)

        for group in active_groups:
            if not _running:
                break

            chat_id = group["chat_id"]

            # Resolve display name
            try:
                chat = await userbot.get_chat(chat_id)
                name = chat.title or str(chat_id)
            except Exception:
                name = str(chat_id)

            print(f"📤 Sending to {name}")

            try:
                for idx, msg in enumerate(messages):
                    cache_key = (chat_id, str(msg["_id"]))
                    if cache_key in sent_this_cycle:
                        continue  # already sent — skip duplicate

                    await _send_one(bot, chat_id, msg)
                    sent_this_cycle.add(cache_key)
                    await _inc_stat(db, "total_sent")
                    print(f"  ✅ Sent")

                    if idx < len(messages) - 1:
                        await asyncio.sleep(random.uniform(2, 3))

            except FloodWait as e:
                print(f"  ⚠️  FloodWait {e.value}s on {name} — skipping")
                await _inc_stat(db, "total_failed")
                await asyncio.sleep(e.value)
            except (SlowModeWait, ChatWriteForbidden,
                    UserBannedInChannel, PeerIdInvalid) as e:
                print(f"  ⚠️  {type(e).__name__} — skipping {name}")
                await _inc_stat(db, "total_failed")
            except Exception as e:
                print(f"  ⚠️  Error on {name}: {e} — skipping")
                await _inc_stat(db, "total_failed")

            if _running:
                await asyncio.sleep(random.uniform(8, 12))

        # ── End of cycle ─────────────────────────────────────────────────────
        sent_this_cycle.clear()
        await _set_stat(db, "last_promotion_time",
                        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))

        if _running:
            print("⏳ Cycle complete — waiting 5 minutes.")
            await asyncio.sleep(300)

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
