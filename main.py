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
    from database import init_db
    from handlers import admin, messages, groups, promo, blacklist, stats, inactive_groups
    from handlers.groups import validate_and_clean_groups
    from core.promotion import restore_state

    # Register handlers
    admin.register(bot_app)
    messages.register(bot_app)
    groups.register(bot_app)
    promo.register(bot_app)
    blacklist.register(bot_app)
    stats.register(bot_app)
    inactive_groups.register(bot_app)

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
