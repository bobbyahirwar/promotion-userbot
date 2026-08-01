import asyncio
import sys
import config  # validates env vars on import

print("🚀 Starting Promotion Userbot...")
print("✅ Configuration Loaded")


async def main():
    from core.bot import bot_app
    from core.userbot import userbot
    from database import init_db
    from handlers import admin, messages, groups

    # Register handlers
    admin.register(bot_app)
    messages.register(bot_app)
    groups.register(bot_app)

    # Start Telegram Bot
    try:
        await bot_app.initialize()
        await bot_app.start()
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
        await bot_app.stop()
        sys.exit(1)

    # Connect MongoDB
    await init_db()

    print("✅ Promotion System Ready")

    # Keep running
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
