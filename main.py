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
    from core.accounts import account_manager
    from core.account_context import account_context
    from database import init_db
    from handlers import accounts, admin, messages, groups, promo, blacklist, stats, inactive_groups
    from handlers import autorotate as autorotate_handler
    import handlers.sync_groups as sync_groups_mod
    import handlers.health as health_mod
    from core import autorotate, promotion
    from handlers.groups import validate_and_clean_groups
    from handlers.sync_groups import sync_joined_groups
    from core.promotion import restore_state
    from core.uptime import set_start_time

    # Register handlers
    accounts.register(bot_app)
    admin.register(bot_app)
    messages.register(bot_app)
    groups.register(bot_app)
    promo.register(bot_app)
    blacklist.register(bot_app)
    stats.register(bot_app)
    inactive_groups.register(bot_app)
    autorotate_handler.register(bot_app)
    sync_groups_mod.register(bot_app)
    health_mod.register(bot_app)

    # Start Telegram Bot
    try:
        await bot_app.initialize()
        await bot_app.start()
        await bot_app.updater.start_polling()
        print("✅ Bot Connected")
    except Exception as e:
        print(f"❌ Bot failed to connect: {e}")
        sys.exit(1)

    # Connect MongoDB
    await init_db()

    try:
        await account_manager.startup()
    except RuntimeError as exc:
        print(f"❌ Account security configuration error: {exc}")
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()
        sys.exit(1)

    set_start_time()

    account_records = await account_manager.list_accounts()
    for record in account_records:
        account_id = int(record["account_id"])
        client = account_manager.clients.get(account_id)
        if not client or not client.is_connected:
            print(f"⚠️ Account {account_id} is disconnected; other accounts remain available.")
            continue
        try:
            with account_context(account_id):
                print(f"🔄 Syncing groups for account {account_id}…")
                total, added, updated, skipped, reactivated = await sync_joined_groups()
                print(
                    f"✅ Account {account_id} group sync — {added} added, {updated} updated, "
                    f"{reactivated} reactivated, {skipped} skipped ({total} dialogs)"
                )
                kept, removed = await validate_and_clean_groups()
                print(
                    f"✅ Account {account_id} groups validated: {kept} kept, {removed} removed."
                )
                await restore_state(bot_app.bot, account_id)
                restored = await autorotate.restore_running_configs(bot_app.bot)
                print(f"✅ Account {account_id} AutoRotate configurations restored: {restored}")
        except Exception as exc:
            print(
                f"❌ Startup initialization failed for account {account_id} "
                f"({type(exc).__name__}); other accounts continue."
            )

    if not account_records:
        print("ℹ️ No Telegram accounts configured. Use /start in the control bot to add one.")

    print("✅ Promotion System Ready")

    # Keep running until cancelled
    try:
        await asyncio.Event().wait()
    finally:
        await autorotate.shutdown()
        await promotion.shutdown()
        await account_manager.shutdown()
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
