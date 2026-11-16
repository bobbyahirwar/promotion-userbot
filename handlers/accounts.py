import logging

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import OWNER_ID
from core.account_context import account_context
from core.accounts import AccountError, DuplicateAccountError, account_manager

log = logging.getLogger(__name__)
ACCOUNT_PAGE_SIZE = 5


def _button(text: str, action: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=f"acct:{action}")


def _main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_button("➕ Add Account", "add")],
        [_button("👥 Manage Accounts", "manage")],
        [_button("❓ Help", "help")],
    ])


async def _main_menu_content() -> tuple[str, InlineKeyboardMarkup]:
    accounts = await account_manager.list_accounts()
    selected_id = account_manager.selected_account_id
    selected = next(
        (account for account in accounts if account["account_id"] == selected_id),
        None,
    )
    current_name = selected["name"] if selected else "None"
    connected_count = sum(
        1
        for account in accounts
        if account_manager.clients.get(int(account["account_id"]))
        and account_manager.clients[int(account["account_id"])].is_connected
    )
    text = (
        f"Connected Accounts: {connected_count}\n"
        f"Current Account: {current_name}"
    )
    return text, _main_keyboard()


async def show_main_menu(message, edit: bool = False) -> None:
    text, keyboard = await _main_menu_content()
    if edit:
        await message.edit_message_text(text, reply_markup=keyboard)
    else:
        await message.reply_text(text, reply_markup=keyboard)


async def _show_manage(query, page: int = 0) -> None:
    accounts = await account_manager.list_accounts()
    if not accounts:
        await query.edit_message_text(
            "Manage Accounts\n\nNo accounts are connected.",
            reply_markup=InlineKeyboardMarkup([
                [_button("➕ Add Account", "add")],
                [_button("⬅️ Main Menu", "home")],
            ]),
        )
        return

    page_count = (len(accounts) + ACCOUNT_PAGE_SIZE - 1) // ACCOUNT_PAGE_SIZE
    page = min(max(0, page), page_count - 1)
    start = page * ACCOUNT_PAGE_SIZE
    page_accounts = accounts[start:start + ACCOUNT_PAGE_SIZE]
    rows = []
    details = [f"Manage Accounts ({len(accounts)} accounts)"]
    for account in page_accounts:
        account_id = int(account["account_id"])
        status = account_manager.status.get(account_id, "Disconnected")
        selected = account_id == account_manager.selected_account_id
        username = f" (@{account['username']})" if account.get("username") else ""
        details.append(
            f"{'✅' if selected else '▫️'} {account.get('name') or account_id}{username}\n"
            f"ID: {account_id}\n"
            f"Status: {status}\n"
            f"Selected: {'Yes' if selected else 'No'}"
        )
        account_buttons = [
            InlineKeyboardButton("Select Account", callback_data=f"acct:select:{account_id}"),
            InlineKeyboardButton("Remove Account", callback_data=f"acct:remove:{account_id}"),
        ]
        rows.append(account_buttons)
        if status != "Connected":
            rows.append([
                InlineKeyboardButton("🔄 Reconnect", callback_data=f"acct:reconnect:{account_id}")
            ])
    if page_count > 1:
        pagination = []
        if page > 0:
            pagination.append(
                InlineKeyboardButton(
                    "◀️ Previous", callback_data=f"acct:manage:{page - 1}"
                )
            )
        pagination.append(
            InlineKeyboardButton(f"{page + 1}/{page_count}", callback_data="acct:manage")
        )
        if page + 1 < page_count:
            pagination.append(
                InlineKeyboardButton(
                    "Next ▶️", callback_data=f"acct:manage:{page + 1}"
                )
            )
        rows.append(pagination)
    rows.extend([
        [_button("➕ Add Account", "add")],
        [_button("⬅️ Main Menu", "home")],
    ])
    await query.edit_message_text(
        "\n\n".join(details),
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _show_help(query) -> None:
    text = (
        "Multi-Account Help\n\n"
        "1. Generate a Pyrogram 2.0.106 StringSession locally with your Telegram API ID "
        "and API hash. For example:\n"
        "from pyrogram import Client\n"
        "import asyncio\n"
        "async def main():\n"
        "    async with Client('session', api_id=API_ID, api_hash='API_HASH') as app:\n"
        "        print(await app.export_session_string())\n"
        "asyncio.run(main())\n\n"
        "Keep the printed StringSession private and remove it from your terminal history.\n"
        "2. Choose Add Account and send the generated StringSession. The bot "
        "validates it, saves it, and deletes the incoming message when Telegram allows.\n"
        "3. Use Manage Accounts → Select Account to choose which account the control "
        "panel commands will manage.\n"
        "4. Use Manage Accounts → Remove Account and confirm to disconnect that account. "
        "Its account-specific data and historical logs are retained.\n\n"
        "Groups, saved messages, settings, AutoRotate configurations, scheduler state, "
        "and logs are isolated by Telegram user ID. Identical chat IDs on different "
        "accounts do not share data. StringSessions are stored in MongoDB; never share "
        "them with anyone."
    )
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [_button("👥 Manage Accounts", "manage")],
            [_button("⬅️ Main Menu", "home")],
        ]),
    )


async def _begin_add(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    if query.message and query.message.chat.type != "private":
        await query.edit_message_text(
            "For your security, start the control bot in a private chat before adding an account."
        )
        return
    context.user_data["awaiting_account_session"] = True
    await query.edit_message_text(
        "Send a Pyrogram 2.0.106 StringSession in your next message.\n"
        "It will be validated before storage. Do not send it to anyone else.",
        reply_markup=InlineKeyboardMarkup([[_button("Cancel", "cancel-add")]]),
    )


async def _callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        await query.answer("Only the bot owner can manage accounts.", show_alert=True)
        return
    await query.answer()
    parts = (query.data or "").split(":")
    if len(parts) < 2 or parts[0] != "acct":
        await query.edit_message_text("Invalid account action.")
        return
    action = parts[1]

    if action == "home":
        context.user_data.pop("awaiting_account_session", None)
        await show_main_menu(query, edit=True)
    elif action == "add":
        await _begin_add(query, context)
    elif action == "cancel-add":
        context.user_data.pop("awaiting_account_session", None)
        await show_main_menu(query, edit=True)
    elif action == "manage":
        try:
            page = int(parts[2]) if len(parts) > 2 else 0
        except ValueError:
            page = 0
        await _show_manage(query, page)
    elif action == "help":
        await _show_help(query)
    elif action in {"select", "remove", "confirm-remove", "reconnect"}:
        if len(parts) != 3:
            await query.edit_message_text("Invalid account action.")
            return
        try:
            account_id = int(parts[2])
        except ValueError:
            await query.edit_message_text("Invalid account identifier.")
            return
        accounts = await account_manager.list_accounts()
        account = next(
            (item for item in accounts if int(item["account_id"]) == account_id),
            None,
        )
        if not account:
            await _show_manage(query)
            return

        if action == "select":
            if not await account_manager.select_account(account_id):
                await query.edit_message_text(
                    "This account is disconnected; reconnect it first.",
                    reply_markup=InlineKeyboardMarkup([
                        [_button("🔄 Reconnect", f"reconnect:{account_id}")],
                        [_button("⬅️ Manage Accounts", "manage")],
                    ]),
                )
            else:
                await show_main_menu(query, edit=True)
        elif action == "remove":
            await query.edit_message_text(
                f"Remove {account.get('name') or account_id} ({account_id})?\n"
                "The account will be disconnected. Its application data and historical "
                "logs will be retained.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "✅ Confirm Removal",
                        callback_data=f"acct:confirm-remove:{account_id}",
                    )],
                    [_button("⬅️ Cancel", "manage")],
                ]),
            )
        elif action == "confirm-remove":
            await account_manager.remove_account(account_id)
            await show_main_menu(query, edit=True)
        else:
            await account_manager.reconnect_account(account_id)
            await _show_manage(query)
    else:
        await query.edit_message_text("This account action is no longer available.")


async def _receive_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    if not context.user_data.pop("awaiting_account_session", False):
        return
    message = update.effective_message
    if not message or not message.text:
        return
    if message.chat.type != "private":
        await message.reply_text("For your security, send account StringSessions only in a private chat.")
        return
    session_string = message.text.strip()
    try:
        await message.delete()
    except TelegramError:
        log.info("Could not delete the incoming account-session message.")

    try:
        account = await account_manager.add_session(session_string, select=True)
    except DuplicateAccountError as exc:
        await context.bot.send_message(chat_id=message.chat_id, text=str(exc))
        menu_text, keyboard = await _main_menu_content()
        await context.bot.send_message(
            chat_id=message.chat_id,
            text=menu_text,
            reply_markup=keyboard,
        )
        return
    except AccountError as exc:
        await context.bot.send_message(chat_id=message.chat_id, text=str(exc))
        return
    finally:
        session_string = ""

    account_id = account["account_id"]
    sync_note = ""
    try:
        from handlers.groups import validate_and_clean_groups
        from handlers.sync_groups import sync_joined_groups

        with account_context(account_id):
            await sync_joined_groups()
            await validate_and_clean_groups()
    except Exception:
        log.exception("Account %s connected but initial group synchronization failed.", account_id)
        sync_note = "\nGroup synchronization will need to be retried."

    username = f" (@{account['username']})" if account.get("username") else ""
    await context.bot.send_message(
        chat_id=message.chat_id,
        text=f"✅ Connected {account['name']}{username}\nAccount ID: {account_id}{sync_note}",
    )
    menu_text, keyboard = await _main_menu_content()
    await context.bot.send_message(
        chat_id=message.chat_id,
        text=menu_text,
        reply_markup=keyboard,
    )


def register(app) -> None:
    app.add_handler(CommandHandler("start", _start))
    app.add_handler(CallbackQueryHandler(_callback, pattern=r"^acct:"))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _receive_session),
        group=1,
    )


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.effective_user.id == OWNER_ID:
        await show_main_menu(update.effective_message)
