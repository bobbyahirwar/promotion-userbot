import asyncio
import os
import sys
import threading
import config  # validates env vars on import

from flask import Flask

print("🚀 Starting Promotion Userbot...")
print("✅ Configuration Loaded")

# ── Flask health server (required for Render port detection) ──────────────────

app = Flask(__name__)

@app.route("/")
def home():
    return "Promotion Bot Running"

def _run_flask():
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        use_reloader=False,
        use_debugger=False,
    )

# Start in a daemon thread so it doesn't block the asyncio loop and exits
# automatically when the main process stops.
threading.Thread(target=_run_flask, daemon=True).start()

# ── Telegram bot ──────────────────────────────────────────────────────────────

async def main():
    from core.bot import bot_app
    from core.userbot import userbot
    from database import init_db, get_db
    from handlers import admin, messages, groups, promo, blacklist, stats, inactive_groups
    import handlers.sync_groups as sync_groups_mod
    from handlers.groups import validate_and_clean_groups
    from handlers.sync_groups import sync_joined_groups
    from core.promotion import restore_state

    # Register handlers
    admin.register(bot_app)
    messages.register(bot_app)
    groups.register(bot_app)
    promo.register(bot_app)
    blacklist.register(bot_app)
    stats.register(bot_app)
    inactive_groups.register(bot_app)
    sync_groups_mod.register(bot_app)

    # Start Telegram Bot
    try:
        await bot_app.initialize()
        await bot_app.start()
        await bot_app.updater.start_polling()
        print("✅ Bot Connected")
    except Exception as e:
        print(f"❌ Bot failed to connect: {e}")
        sys.exit(1)

    # Start Pyrogram User Session
    try:
        await userbot.start()
        print("✅ User Session Connected")
    except Exception as e:
        print(f"❌ User Session failed to connect: {e}")
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()
        sys.exit(1)

    # Connect MongoDB
    await init_db()

    # ── Account-change detection ──────────────────────────────────────────────
    # If the SESSION_STRING belongs to a different Telegram account than the
    # one stored in MongoDB, all previous group data is stale — clear it and
    # start fresh before syncing.
    db = get_db()
    me = await userbot.get_me()
    current_user_id = me.id

    stored_doc = await db.state.find_one({"key": "account_user_id"})
    stored_user_id = stored_doc["value"] if stored_doc else None

    if stored_user_id is not None and stored_user_id != current_user_id:
        print(
            f"⚠️  Userbot account changed "
            f"({stored_user_id} → {current_user_id}). "
            f"Clearing stale data…"
        )
        await db.groups.delete_many({})
        await db.inactive_groups.delete_many({})
        await db.stats.delete_many(
            {"key": {"$in": ["total_sent", "total_failed", "last_promotion_time"]}}
        )
        print("✅ Stale groups, inactive list, and stats cleared.")

    # Persist the current account ID so future startups can detect a change.
    await db.state.update_one(
        {"key": "account_user_id"},
        {"$set": {"value": current_user_id}},
        upsert=True,
    )

    # ── Sync joined groups from Telegram ─────────────────────────────────────
    # Upserts every group/supergroup the userbot is a member of.  New groups
    # are added; existing ones have their title/username refreshed.  This
    # runs on every startup so the DB stays in sync with reality.
    print("🔄 Syncing joined groups from Telegram…")
    sync_total, sync_added, sync_updated, sync_skipped = await sync_joined_groups()
    print(
        f"✅ Group sync done — "
        f"{sync_added} added, {sync_updated} updated, "
        f"{sync_skipped} skipped ({sync_total} dialogs total)"
    )

    # Validate stored groups — remove any that are permanently inaccessible.
    # This runs once per startup and never crashes the bot, even if every
    # stored group is invalid.
    print("🔍 Validating stored groups...")
    kept, removed = await validate_and_clean_groups()
    print(f"✅ Groups validated: {kept} kept, {removed} removed.")

    # Resume promotion if it was running before restart
    await restore_state(bot_app.bot)

    print("✅ Promotion System Ready")

    # Keep running until cancelled
    try:
        await asyncio.Event().wait()
    finally:
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()
        await userbot.stop()


if __name__ == "__main__":
    asyncio.run(main())
