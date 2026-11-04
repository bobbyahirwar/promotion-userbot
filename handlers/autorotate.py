import asyncio
import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import OWNER_ID
from core import autorotate
from database import get_db
from handlers.admin import owner_only
from handlers.messages import _preview

MAX_REPLY_LENGTH = 3500
PAGE_SIZE = 8
_callback_ids: set[str] = set()
_callback_order: list[str] = []
_callback_locks: dict[int, asyncio.Lock] = {}
_ui_drafts: dict[int, dict] = {}
_custom_interval_inputs: dict[int, dict] = {}
_log = logging.getLogger(__name__)

_INTERVAL_BUTTONS = (
    ("10 seconds", 10),
    ("20 seconds", 20),
    ("30 seconds", 30),
    ("1 minute", 60),
    ("2 minutes", 120),
    ("5 minutes", 300),
)


def _chunks(text: str) -> list[str]:
    lines = text.splitlines()
    chunks = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > MAX_REPLY_LENGTH and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [""]


async def _reply(update: Update, text: str) -> None:
    for chunk in _chunks(text):
        await update.message.reply_text(chunk)


def _button(text: str, action: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=f"ar:{action}")


def _callback(action: str, *parts) -> str:
    return ":".join(("ar", action, *(str(part) for part in parts)))


def _keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)


async def _safe_answer(query, text: str | None = None, show_alert: bool = False) -> bool:
    try:
        await query.answer(text=text, show_alert=show_alert)
        return True
    except BadRequest as exc:
        _log.debug("AutoRotate callback answer expired: %s", exc)
        return False


async def _edit_query(query, text: str, reply_markup=None) -> None:
    try:
        await query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            _log.info("Could not edit AutoRotate menu message: %s", exc)


def _owner_id(update: Update) -> int | None:
    user = getattr(update, "effective_user", None) or getattr(update, "from_user", None)
    return user.id if user else None


def _interval_label(seconds: int) -> str:
    labels = {10: "10 seconds", 20: "20 seconds", 30: "30 seconds", 60: "1 minute", 120: "2 minutes", 300: "5 minutes"}
    return labels.get(seconds, f"{seconds} seconds")


def _dedupe(values: list) -> list:
    return list(dict.fromkeys(values))


def _toggle_selection(selection: list, item) -> list:
    updated = list(selection)
    if item in updated:
        updated.remove(item)
    else:
        updated.append(item)
    return updated


def _select_all(selection: list, eligible: list) -> list:
    return _dedupe([*selection, *eligible])


def _clear_selection(selection: list, eligible: list) -> list:
    blocked = set(eligible)
    return [item for item in selection if item not in blocked]


def _page_bounds(count: int, page: int) -> tuple[int, int, int]:
    page_count = max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(0, page), page_count - 1)
    start = page * PAGE_SIZE
    return start, min(start + PAGE_SIZE, count), page_count


def _row_label(text: str, limit: int = 36) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


async def _show_home(query, text: str = "Auto Message Rotation") -> None:
    rows = [
        [_button("➕ Create New Configuration", "create")],
        [_button("🗂 Manage Configurations", "manage:0")],
        [_button("🟢 Active Configurations", "active:0")],
        [_button("❔ Help", "help")],
    ]
    await _edit_query(query, text, _keyboard(rows))


async def _show_help(query) -> None:
    await _edit_query(
        query,
        "Auto Message Rotation\n\n"
        "Create a configuration, select only groups you explicitly authorize, "
        "choose saved messages and set an interval. Review the summary before saving.\n\n"
        "Group posting requires at least a 1-minute interval and sends at least "
        "60 seconds apart globally. Short intervals are available only for private tests.\n"
        "FloodWait pauses rotation; the bot will not retry before Telegram's cooldown expires.",
        _keyboard([[_button("⬅️ Main Menu", "home")]]),
    )


async def _load_pickable_groups() -> tuple[list[dict], set[int]]:
    groups, blocked = await _groups_and_blocked()
    return [group for group in groups if group["chat_id"] not in blocked], blocked


async def _load_pickable_messages() -> list[dict]:
    return await get_db().messages.find().sort("_id", 1).to_list(100)


async def _show_group_picker(query, user_id: int, page: int = 0, notice: str | None = None) -> None:
    draft = _ui_drafts.get(user_id)
    if not draft:
        await _show_home(query, "This selection expired. Start again.")
        return
    groups, _ = await _load_pickable_groups()
    eligible_ids = {group["chat_id"] for group in groups}
    unavailable_selected = sum(chat_id not in eligible_ids for chat_id in draft.get("group_ids", []))
    selected = [chat_id for chat_id in draft.get("group_ids", []) if chat_id in eligible_ids]
    draft["group_ids"] = selected
    start, end, page_count = _page_bounds(len(groups), page)
    page = min(max(0, page), page_count - 1)
    title = (
        f"Select groups ({len(selected)} selected)\n"
        "Only active groups from syncgroups are available."
        + (f"\n{unavailable_selected} unavailable selection(s) removed." if unavailable_selected else "")
    )
    if notice:
        title = f"{notice}\n\n{title}"
    rows = []
    for index in range(start, end):
        group = groups[index]
        chat_id = group["chat_id"]
        marker = "☑️" if chat_id in selected else "⬜"
        rows.append([InlineKeyboardButton(
            f"{marker} {_row_label(_group_name(group), 31)}",
            callback_data=_callback("gt", chat_id, page),
        )])

    controls = [
        InlineKeyboardButton("Select All", callback_data=_callback("ga", page)),
        InlineKeyboardButton("Clear Selection", callback_data=_callback("gc", page)),
    ]
    rows.append(controls)
    if page_count > 1:
        pagination = []
        if page > 0:
            pagination.append(InlineKeyboardButton("◀️", callback_data=_callback("gp", page - 1)))
        pagination.append(InlineKeyboardButton(f"{page + 1}/{page_count}", callback_data=_callback("noop")))
        if page + 1 < page_count:
            pagination.append(InlineKeyboardButton("▶️", callback_data=_callback("gp", page + 1)))
        rows.append(pagination)
    rows.append([
        _button("⬅️ Back", "gb"),
        InlineKeyboardButton("Next ➡️", callback_data=_callback("gn", page)),
    ])
    await _edit_query(query, title, _keyboard(rows))


async def _show_message_picker(query, user_id: int, page: int = 0, notice: str | None = None) -> None:
    draft = _ui_drafts.get(user_id)
    if not draft:
        await _show_home(query, "This selection expired. Start again.")
        return
    messages = await _load_pickable_messages()
    selected = draft.setdefault("message_ids", [])
    start, end, page_count = _page_bounds(len(messages), page)
    page = min(max(0, page), page_count - 1)
    rows = []
    for index in range(start, end):
        message = messages[index]
        message_id = message["_id"]
        marker = "☑️" if message_id in selected else "⬜"
        rows.append([InlineKeyboardButton(
            f"{marker} {_row_label(_preview(message), 32)}",
            callback_data=_callback("mt", message_id, page),
        )])
    rows.append([
        InlineKeyboardButton("Select All", callback_data=_callback("ma", page)),
        InlineKeyboardButton("Clear Selection", callback_data=_callback("mc", page)),
    ])
    if page_count > 1:
        pagination = []
        if page > 0:
            pagination.append(InlineKeyboardButton("◀️", callback_data=_callback("mp", page - 1)))
        pagination.append(InlineKeyboardButton(f"{page + 1}/{page_count}", callback_data=_callback("noop")))
        if page + 1 < page_count:
            pagination.append(InlineKeyboardButton("▶️", callback_data=_callback("mp", page + 1)))
        rows.append(pagination)
    back_action = "mg" if draft.get("mode") == "new" else "cfg"
    back_target = draft.get("config_id", "")
    rows.append([
        InlineKeyboardButton(
            "⬅️ Back",
            callback_data=_callback(back_action, back_target) if back_target else _callback(back_action),
        ),
        InlineKeyboardButton("Next ➡️", callback_data=_callback("mn", page)),
    ])
    title = f"Select saved messages ({len(selected)} selected)\nSelection order is rotation order."
    if notice:
        title = f"{notice}\n\n{title}"
    await _edit_query(
        query,
        title,
        _keyboard(rows),
    )


async def _show_interval_picker(query, user_id: int) -> None:
    draft = _ui_drafts.get(user_id)
    if not draft:
        await _show_home(query, "This selection expired. Start again.")
        return
    rows = []
    for index in range(0, len(_INTERVAL_BUTTONS), 2):
        rows.append([
            InlineKeyboardButton(
                _INTERVAL_BUTTONS[item_index][0],
                callback_data=_callback("is", _INTERVAL_BUTTONS[item_index][1]),
            )
            for item_index in range(index, min(index + 2, len(_INTERVAL_BUTTONS)))
        ])
    rows.append([_button("Custom interval", "ic")])
    back_action = "mb" if draft.get("mode") == "new" else "cfg"
    config_id = draft.get("config_id")
    rows.append([
        InlineKeyboardButton(
            "⬅️ Back",
            callback_data=_callback(back_action, config_id) if config_id else _callback(back_action),
        )
    ])
    await _edit_query(
        query,
        "Choose the rotation interval.\n"
        "10–30 seconds are for private tests only; group rotation requires at least 1 minute.",
        _keyboard(rows),
    )


async def _show_configuration_list(query, page: int = 0, active_only: bool = False) -> None:
    query_filter = {"status": "running"} if active_only else {}
    configs = await get_db().autorotate_configs.find(query_filter).sort("created_at", -1).to_list(None)
    start, end, page_count = _page_bounds(len(configs), page)
    title = "Active Configurations" if active_only else "Manage Configurations"
    if not configs:
        await _edit_query(
            query,
            f"{title}\n\nNo configurations found.",
            _keyboard([[_button("⬅️ Main Menu", "home")]]),
        )
        return

    rows = []
    for config in configs[start:end]:
        status = config.get("status", "stopped")
        draft = _ui_drafts.get(OWNER_ID)
        if draft and draft.get("config_id") == str(config["_id"]):
            status = "pending confirmation"
        label = f"{config.get('name', 'Rotation')} · {status} · {len(config.get('group_ids', []))} groups"
        rows.append([InlineKeyboardButton(
            _row_label(label, 48),
            callback_data=_callback("cfg", config["_id"]),
        )])
    if page_count > 1:
        controls = []
        if page > 0:
            controls.append(InlineKeyboardButton("◀️", callback_data=_callback("active" if active_only else "manage", page - 1)))
        controls.append(InlineKeyboardButton(f"{page + 1}/{page_count}", callback_data=_callback("noop")))
        if page + 1 < page_count:
            controls.append(InlineKeyboardButton("▶️", callback_data=_callback("active" if active_only else "manage", page + 1)))
        rows.append(controls)
    rows.append([_button("⬅️ Main Menu", "home")])
    await _edit_query(query, title, _keyboard(rows))


async def _configuration_text(config: dict) -> str:
    db = get_db()
    group_ids = config.get("group_ids", [])
    groups = await db.groups.find({"chat_id": {"$in": group_ids}}).to_list(len(group_ids))
    by_id = {group["chat_id"]: group for group in groups}
    group_lines = [
        f"• {_group_name(by_id[group_id])} ({group_id})" if group_id in by_id else f"• {group_id} (missing)"
        for group_id in group_ids[:10]
    ]
    if len(group_ids) > 10:
        group_lines.append(f"• …and {len(group_ids) - 10} more")

    message_ids = config.get("message_ids", [])
    messages = await db.messages.find({"_id": {"$in": message_ids}}).to_list(len(message_ids))
    message_by_id = {message["_id"]: message for message in messages}
    message_lines = [
        f"• {_row_label(_preview(message_by_id[message_id]), 48)}" if message_id in message_by_id
        else f"• {message_id} (deleted)"
        for message_id in message_ids[:10]
    ]
    if len(message_ids) > 10:
        message_lines.append(f"• …and {len(message_ids) - 10} more")

    return (
        f"{config.get('name', 'AutoRotate')}\n"
        f"ID: {config['_id']}\n"
        f"Interval: {_interval_label(int(config.get('interval_seconds', 300)))}\n"
        f"Status: {config.get('status', 'stopped')}"
        + (" (private test)" if config.get("test_mode") and config.get("status") == "running" else "")
        + f"\n\nGroups ({len(group_ids)}):\n"
        + ("\n".join(group_lines) if group_lines else "• None selected")
        + f"\n\nSaved messages ({len(message_ids)}):\n"
        + ("\n".join(message_lines) if message_lines else "• None selected")
        + (f"\n\nLast error: {config['last_error']}" if config.get("last_error") else "")
    )


async def _show_configuration(query, config_id: str, notice: str | None = None) -> None:
    config = await autorotate.get_config(config_id)
    if not config:
        await _show_home(query, "Configuration not found.")
        return
    text = await _configuration_text(config)
    if notice:
        text = f"{notice}\n\n{text}"

    config_id = str(config["_id"])
    status = config.get("status", "stopped")
    rows = []
    pending_draft = _ui_drafts.get(OWNER_ID)
    if pending_draft and pending_draft.get("config_id") == config_id and pending_draft.get("mode") == "new":
        rows.append([InlineKeyboardButton("✅ Confirm & Save", callback_data=_callback("ok", config_id))])
        rows.append([InlineKeyboardButton("Cancel Draft", callback_data=_callback("cancel", config_id))])
    elif status == "running":
        rows.extend([
            [InlineKeyboardButton("⏸ Pause", callback_data=_callback("pause", config_id)),
             InlineKeyboardButton("⏹ Stop", callback_data=_callback("stop", config_id))],
        ])
    elif status == "paused":
        rows.extend([
            [InlineKeyboardButton("▶️ Resume", callback_data=_callback("resume", config_id)),
             InlineKeyboardButton("⏹ Stop", callback_data=_callback("stop", config_id))],
            [InlineKeyboardButton("👥 Edit Groups", callback_data=_callback("eg", config_id)),
             InlineKeyboardButton("✉️ Edit Messages", callback_data=_callback("em", config_id))],
            [InlineKeyboardButton("⏱ Change Interval", callback_data=_callback("ei", config_id))],
        ])
    else:
        rows.extend([
            [InlineKeyboardButton("▶️ Start", callback_data=_callback("start", config_id)),
             InlineKeyboardButton("🧪 Private Test", callback_data=_callback("test", config_id))],
            [InlineKeyboardButton("👥 Edit Groups", callback_data=_callback("eg", config_id)),
             InlineKeyboardButton("✉️ Edit Messages", callback_data=_callback("em", config_id))],
            [InlineKeyboardButton("⏱ Change Interval", callback_data=_callback("ei", config_id)),
             InlineKeyboardButton("🗑 Delete", callback_data=_callback("delq", config_id))],
        ])
    if status in {"running", "paused"}:
        rows.append([InlineKeyboardButton("🗑 Delete", callback_data=_callback("delq", config_id))])
    rows.append([_button("⬅️ Configurations", "manage:0"), _button("🏠 Main Menu", "home")])
    await _edit_query(query, text, _keyboard(rows))


async def _begin_group_edit(query, user_id: int, config: dict, mode: str) -> None:
    _ui_drafts[user_id] = {
        "mode": mode,
        "config_id": str(config["_id"]),
        "group_ids": list(config.get("group_ids", [])),
        "message_ids": list(config.get("message_ids", [])),
        "interval_seconds": int(config.get("interval_seconds", 300)),
        "name": config.get("name", "Rotation"),
    }
    await _show_group_picker(query, user_id)


async def _begin_message_edit(query, user_id: int, config: dict) -> None:
    _ui_drafts[user_id] = {
        "mode": "edit_messages",
        "config_id": str(config["_id"]),
        "group_ids": list(config.get("group_ids", [])),
        "message_ids": list(config.get("message_ids", [])),
        "interval_seconds": int(config.get("interval_seconds", 300)),
        "name": config.get("name", "Rotation"),
    }
    await _show_message_picker(query, user_id)


async def _begin_interval_edit(query, user_id: int, config: dict) -> None:
    _ui_drafts[user_id] = {
        "mode": "edit_interval",
        "config_id": str(config["_id"]),
        "group_ids": list(config.get("group_ids", [])),
        "message_ids": list(config.get("message_ids", [])),
        "interval_seconds": int(config.get("interval_seconds", 300)),
        "name": config.get("name", "Rotation"),
    }
    await _show_interval_picker(query, user_id)


class _MessageEditor:
    def __init__(self, bot, chat_id: int, message_id: int):
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id

    async def edit_message_text(self, text: str, reply_markup=None, disable_web_page_preview=True):
        return await self.bot.edit_message_text(
            chat_id=self.chat_id,
            message_id=self.message_id,
            text=text,
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
        )


async def _save_new_config(draft: dict) -> dict:
    config = draft.get("config")
    is_new = config is None
    if is_new:
        config = await autorotate.create_config(draft["name"])
    updated = await autorotate.edit_config(
        str(config["_id"]),
        {
            "group_ids": draft["group_ids"],
            "message_ids": draft["message_ids"],
            "interval_seconds": draft["interval_seconds"],
            "status": "stopped",
        },
        reset_rotation=True,
    )
    if not updated:
        if is_new:
            await autorotate.delete_config(str(config["_id"]))
        raise RuntimeError("Could not save the selected AutoRotate configuration.")
    draft["config"] = config
    draft["config_id"] = str(config["_id"])
    if await autorotate._is_duplicate({
        **config,
        "group_ids": draft["group_ids"],
        "message_ids": draft["message_ids"],
        "interval_seconds": draft["interval_seconds"],
        "test_mode": False,
    }):
        await autorotate.delete_config(str(config["_id"]))
        draft.pop("config", None)
        draft.pop("config_id", None)
        raise ValueError("An identical configuration already exists.")
    return config


async def _show_creation_summary(query, draft: dict) -> None:
    await _save_new_config(draft)
    await _edit_query(
        query,
        "Review configuration\n\n"
        f"Name: {draft['name']}\n"
        f"ID: {draft['config_id']}\n"
        f"Groups: {len(draft['group_ids'])}\n"
        f"Saved messages: {len(draft['message_ids'])}\n"
        f"Interval: {_interval_label(draft['interval_seconds'])}\n"
        "Status: Pending confirmation",
        _keyboard([
            [InlineKeyboardButton("✅ Confirm & Save", callback_data=_callback("ok", draft["config_id"]))],
            [
                InlineKeyboardButton("⬅️ Back", callback_data=_callback("ib")),
                InlineKeyboardButton("Cancel", callback_data=_callback("cancel", draft["config_id"])),
            ],
        ]),
    )


async def _show_custom_interval_prompt(query, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = _ui_drafts.get(user_id)
    if not draft:
        await _show_home(query, "This selection expired. Start again.")
        return
    _custom_interval_inputs[user_id] = {
        "chat_id": query.message.chat_id,
        "message_id": query.message.message_id,
    }
    await _edit_query(
        query,
        "Send a custom interval in seconds (10–86400).\n"
        "Group posting requires at least 60 seconds.",
        _keyboard([[_button("Cancel", "ib")]]),
    )


async def _handle_custom_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = _owner_id(update)
    if user_id != OWNER_ID:
        return
    pending = _custom_interval_inputs.get(user_id)
    message = update.effective_message
    if not pending or not message or message.chat_id != pending["chat_id"]:
        return
    text = (message.text or "").strip()
    editor = _MessageEditor(context.bot, pending["chat_id"], pending["message_id"])
    try:
        seconds = int(text)
    except ValueError:
        await editor.edit_message_text(
            "Enter a whole number of seconds between 10 and 86400.",
            reply_markup=_keyboard([[_button("Cancel", "ib")]]),
        )
        return
    if not 10 <= seconds <= 86400:
        await editor.edit_message_text(
            "Interval must be between 10 and 86400 seconds.",
            reply_markup=_keyboard([[_button("Cancel", "ib")]]),
        )
        return

    _custom_interval_inputs.pop(user_id, None)
    draft = _ui_drafts.get(user_id)
    if not draft:
        await editor.edit_message_text("This selection expired. Send /autorotate to start again.")
        return
    draft["interval_seconds"] = seconds
    try:
        if draft.get("mode") == "new":
            await _show_creation_summary(editor, draft)
        else:
            updated = await autorotate.edit_config(
                draft["config_id"],
                {"interval_seconds": seconds},
            )
            _ui_drafts.pop(user_id, None)
            if not updated:
                await editor.edit_message_text("Pause or stop the configuration before changing its interval.")
                return
            await _show_configuration(editor, draft["config_id"], "✅ Interval updated.")
    except Exception as exc:
        _log.exception("Could not apply custom AutoRotate interval")
        await editor.edit_message_text(
            f"Could not update the interval: {exc}",
            reply_markup=_keyboard([[_button("⬅️ Main Menu", "home")]]),
        )


async def _save_group_edit(query, user_id: int) -> None:
    draft = _ui_drafts.get(user_id)
    if not draft:
        await _show_home(query, "This selection expired. Start again.")
        return
    updated = await autorotate.edit_config(
        draft["config_id"],
        {"group_ids": draft["group_ids"]},
    )
    config_id = draft["config_id"]
    _ui_drafts.pop(user_id, None)
    if not updated:
        await _show_configuration(query, config_id, "Pause or stop this configuration before editing its groups.")
        return
    await _show_configuration(query, config_id, "✅ Group selection saved.")


async def _save_message_edit(query, user_id: int) -> None:
    draft = _ui_drafts.get(user_id)
    if not draft:
        await _show_home(query, "This selection expired. Start again.")
        return
    updated = await autorotate.edit_config(
        draft["config_id"],
        {"message_ids": draft["message_ids"]},
        reset_rotation=True,
    )
    config_id = draft["config_id"]
    _ui_drafts.pop(user_id, None)
    if not updated:
        await _show_configuration(query, config_id, "Pause or stop this configuration before editing its messages.")
        return
    await _show_configuration(query, config_id, "✅ Message selection saved.")


async def _run_config_action(query, context: ContextTypes.DEFAULT_TYPE, action: str, config_id: str) -> None:
    notice = None
    if action == "start":
        success, result = await autorotate.start_config(config_id, context.bot)
        notice = ("✅ " if success else "⚠️ ") + result
    elif action == "test":
        success, result = await autorotate.start_config(config_id, context.bot, test_mode=True)
        notice = ("✅ " if success else "⚠️ ") + result
    elif action == "pause":
        notice = "⏸ Configuration paused." if await autorotate.pause_config(config_id) else "Configuration is not running."
    elif action == "resume":
        success, result = await autorotate.resume_config(config_id, context.bot)
        notice = ("✅ " if success else "⚠️ ") + result
    elif action == "stop":
        notice = "⏹ Configuration stopped." if await autorotate.stop_config(config_id) else "Configuration not found."
    await _show_configuration(query, config_id, notice)


async def _dispatch_callback(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = (query.data or "").split(":")
    if len(data) < 2 or data[0] != "ar":
        return
    action = data[1]
    user_id = _owner_id(query)

    if action == "home":
        _ui_drafts.pop(user_id, None)
        _custom_interval_inputs.pop(user_id, None)
        await _show_home(query)
    elif action == "help":
        await _show_help(query)
    elif action == "create":
        _ui_drafts[user_id] = {
            "mode": "new",
            "name": f"Rotation {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}",
            "group_ids": [],
            "message_ids": [],
            "interval_seconds": 300,
        }
        await _show_group_picker(query, user_id)
    elif action in {"manage", "active"}:
        page = int(data[2]) if len(data) > 2 else 0
        await _show_configuration_list(query, page, active_only=(action == "active"))
    elif action == "noop":
        return
    elif action == "cfg":
        await _show_configuration(query, data[2])
    elif action == "gp":
        await _show_group_picker(query, user_id, int(data[2]))
    elif action in {"gt", "ga", "gc"}:
        groups, _ = await _load_pickable_groups()
        draft = _ui_drafts.get(user_id)
        if not draft:
            await _show_home(query, "This selection expired. Start again.")
            return
        page = int(data[3]) if action == "gt" and len(data) > 3 else int(data[-1]) if len(data) > 2 else 0
        eligible = [group["chat_id"] for group in groups]
        if action == "gt":
            chat_id = int(data[2])
            if chat_id not in eligible:
                await _show_group_picker(query, user_id, page)
                return
            draft["group_ids"] = _toggle_selection(draft["group_ids"], chat_id)
        elif action == "ga":
            draft["group_ids"] = _select_all(draft["group_ids"], eligible)
        else:
            draft["group_ids"] = _clear_selection(draft["group_ids"], eligible)
        await _show_group_picker(query, user_id, page)
    elif action == "gb":
        draft = _ui_drafts.pop(user_id, None)
        _custom_interval_inputs.pop(user_id, None)
        if draft and draft.get("config_id"):
            await _show_configuration(query, draft["config_id"])
        else:
            await _show_home(query)
    elif action == "gn":
        draft = _ui_drafts.get(user_id)
        if not draft:
            await _show_home(query, "This selection expired. Start again.")
        elif not draft["group_ids"] and draft.get("mode") == "new":
            await _show_group_picker(
                query,
                user_id,
                notice="Select at least one group to continue.",
            )
        elif draft.get("mode") == "edit_groups":
            await _save_group_edit(query, user_id)
        else:
            draft["message_ids"] = []
            await _show_message_picker(query, user_id)
    elif action == "mp":
        await _show_message_picker(query, user_id, int(data[2]))
    elif action in {"mt", "ma", "mc"}:
        messages = await _load_pickable_messages()
        draft = _ui_drafts.get(user_id)
        if not draft:
            await _show_home(query, "This selection expired. Start again.")
            return
        page = int(data[3]) if action == "mt" and len(data) > 3 else int(data[-1]) if len(data) > 2 else 0
        eligible = [message["_id"] for message in messages]
        if action == "mt":
            message_id = data[2]
            message = next((item for item in messages if str(item["_id"]) == message_id), None)
            if message is None:
                await _show_message_picker(query, user_id, page)
                return
            draft["message_ids"] = _toggle_selection(draft["message_ids"], message["_id"])
        elif action == "ma":
            draft["message_ids"] = _select_all(draft["message_ids"], eligible)
        else:
            draft["message_ids"] = _clear_selection(draft["message_ids"], eligible)
        await _show_message_picker(query, user_id, page)
    elif action == "mg":
        draft = _ui_drafts.get(user_id)
        if not draft:
            await _show_home(query, "This selection expired. Start again.")
        elif draft.get("mode") == "new":
            await _show_group_picker(query, user_id)
        else:
            _ui_drafts.pop(user_id, None)
            await _show_configuration(query, draft["config_id"])
    elif action == "mn":
        draft = _ui_drafts.get(user_id)
        if not draft:
            await _show_home(query, "This selection expired. Start again.")
        elif not draft["message_ids"] and draft.get("mode") == "new":
            await _show_message_picker(
                query,
                user_id,
                notice="Select at least one saved message to continue.",
            )
        elif draft.get("mode") == "edit_messages":
            await _save_message_edit(query, user_id)
        else:
            await _show_interval_picker(query, user_id)
    elif action == "mb":
        draft = _ui_drafts.get(user_id)
        if not draft:
            await _show_home(query, "This selection expired. Start again.")
        elif draft.get("mode") == "new":
            await _show_message_picker(query, user_id)
        else:
            _ui_drafts.pop(user_id, None)
            await _show_configuration(query, draft["config_id"])
    elif action == "is":
        seconds = int(data[2])
        draft = _ui_drafts.get(user_id)
        if not draft:
            await _show_home(query, "This selection expired. Start again.")
        elif draft.get("mode") == "new":
            draft["interval_seconds"] = seconds
            await _show_creation_summary(query, draft)
        else:
            updated = await autorotate.edit_config(
                draft["config_id"],
                {"interval_seconds": seconds},
            )
            config_id = draft["config_id"]
            _ui_drafts.pop(user_id, None)
            await _show_configuration(
                query,
                config_id,
                "✅ Interval updated." if updated else "Pause or stop this configuration before changing its interval.",
            )
    elif action == "ic":
        await _show_custom_interval_prompt(query, user_id, context)
    elif action == "ib":
        _custom_interval_inputs.pop(user_id, None)
        draft = _ui_drafts.get(user_id)
        if not draft:
            await _show_home(query, "This selection expired. Start again.")
        elif draft.get("mode") == "new":
            await _show_interval_picker(query, user_id)
        else:
            await _show_configuration(query, draft["config_id"])
    elif action == "ok":
        config_id = data[2]
        updated = await autorotate.edit_config(
            config_id,
            {"status": "stopped"},
        )
        _ui_drafts.pop(user_id, None)
        _custom_interval_inputs.pop(user_id, None)
        await _show_configuration(
            query,
            config_id,
            "✅ Configuration saved." if updated else "Configuration could not be confirmed.",
        )
    elif action == "cancel":
        config_id = data[2]
        await autorotate.delete_config(config_id)
        _ui_drafts.pop(user_id, None)
        _custom_interval_inputs.pop(user_id, None)
        await _show_home(query, "Configuration cancelled.")
    elif action in {"start", "test", "pause", "resume", "stop"}:
        await _run_config_action(query, context, action, data[2])
    elif action == "eg":
        config = await autorotate.get_config(data[2])
        if not config:
            await _show_home(query, "Configuration not found.")
        else:
            await _begin_group_edit(query, user_id, config, "edit_groups")
    elif action == "em":
        config = await autorotate.get_config(data[2])
        if not config:
            await _show_home(query, "Configuration not found.")
        else:
            await _begin_message_edit(query, user_id, config)
    elif action == "ei":
        config = await autorotate.get_config(data[2])
        if not config:
            await _show_home(query, "Configuration not found.")
        else:
            await _begin_interval_edit(query, user_id, config)
    elif action == "delq":
        config_id = data[2]
        await _edit_query(
            query,
            "Delete this AutoRotate configuration? This cannot be undone.",
            _keyboard([
                [InlineKeyboardButton("🗑 Yes, delete", callback_data=_callback("dely", config_id))],
                [InlineKeyboardButton("⬅️ Back", callback_data=_callback("cfg", config_id))],
            ]),
        )
    elif action == "dely":
        await autorotate.delete_config(data[2])
        await _show_configuration_list(query)
    else:
        await _safe_answer(query, "This menu action is no longer available.", show_alert=True)


async def callback_autorotate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if _owner_id(update) != OWNER_ID:
        await _safe_answer(query, "Only the bot owner can use AutoRotate controls.", show_alert=True)
        return
    callback_id = query.id
    if callback_id in _callback_ids:
        await _safe_answer(query, "This action was already processed.")
        return
    _callback_ids.add(callback_id)
    _callback_order.append(callback_id)
    if len(_callback_order) > 1000:
        _callback_ids.discard(_callback_order.pop(0))

    user_id = OWNER_ID
    lock = _callback_locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        action = (query.data or "").split(":", 2)[1]
        loading_actions = {
            "create", "manage", "active", "cfg", "gp", "gt", "ga", "gc",
            "mt", "ma", "mc", "mp", "gn", "mn", "is", "ok", "start",
            "test", "pause", "resume", "stop", "eg", "em", "ei", "dely",
        }
        delivered = await _safe_answer(query, "Loading…" if action in loading_actions else None)
        if not delivered:
            return
        try:
            await _dispatch_callback(query, context)
        except BadRequest as exc:
            _log.info("AutoRotate callback expired or unavailable: %s", exc)
        except Exception as exc:
            _log.exception("AutoRotate callback failed")
            await _edit_query(
                query,
                f"AutoRotate operation failed: {exc}",
                _keyboard([[_button("⬅️ Main Menu", "home")]]),
            )


async def cmd_autorotate_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Auto Message Rotation",
        reply_markup=_keyboard([
            [_button("➕ Create New Configuration", "create")],
            [_button("🗂 Manage Configurations", "manage:0")],
            [_button("🟢 Active Configurations", "active:0")],
            [_button("❔ Help", "help")],
        ]),
    )


async def _load_config(update: Update, config_id: str) -> dict | None:
    try:
        config = await autorotate.get_config(config_id)
    except ValueError as exc:
        await _reply(update, str(exc))
        return None
    if not config:
        await _reply(update, "Configuration not found.")
        return None
    return config


def _group_name(group: dict) -> str:
    title = (group.get("title") or "").replace("\n", " ").strip()
    return title[:45] or str(group["chat_id"])


async def _groups_and_blocked() -> tuple[list[dict], set[int]]:
    db = get_db()
    groups = await db.groups.find().sort("chat_id", 1).to_list(None)
    inactive_docs = await db.inactive_groups.find({}, {"chat_id": 1}).to_list(None)
    blacklist_docs = await db.blacklist.find({}, {"chat_id": 1}).to_list(None)
    blocked = {doc["chat_id"] for doc in inactive_docs + blacklist_docs}
    return groups, blocked


async def _show_groups(update: Update, config: dict, groups: list[dict], blocked: set[int]) -> None:
    selected = set(config.get("group_ids", []))
    lines = [
        f"Groups for {config['name']} ({str(config['_id'])})",
        "Use: /autorotate groups <id> all|none|1,2 or +1,-2",
        "Only owner-selected, active, non-blacklisted groups can run.",
        "",
    ]
    for index, group in enumerate(groups, 1):
        chat_id = group["chat_id"]
        marker = "✓" if chat_id in selected else " "
        status = " [inactive/blacklisted]" if chat_id in blocked else ""
        lines.append(f"{marker} {index}. {_group_name(group)} ({chat_id}){status}")
    await _reply(update, "\n".join(lines))


async def _show_messages(update: Update, config: dict, messages: list[dict]) -> None:
    selected_ids = config.get("message_ids", [])
    existing_ids = {message["_id"] for message in messages}
    lines = [
        f"Saved messages for {config['name']} ({str(config['_id'])})",
        "Use: /autorotate messages <id> all|none|1,2 or +1,-2",
        "Selection order is the rotation order.",
        "",
    ]
    for index, message in enumerate(messages, 1):
        marker = "✓" if message["_id"] in selected_ids else " "
        lines.append(f"{marker} {index}. {_preview(message)}")
    missing_count = sum(message_id not in existing_ids for message_id in selected_ids)
    if missing_count:
        lines.append(f"\n⚠️ {missing_count} selected message(s) have been deleted.")
    await _reply(update, "\n".join(lines))


async def _parse_selection(
    update: Update,
    words: list[str],
    items: list,
    selected: list,
    kind: str,
    blocked_groups: set[int] | None = None,
) -> list | None:
    if not words:
        return None
    selection = " ".join(words).replace(" ", "")
    if selection.lower() in {"all", "none"}:
        if selection.lower() == "none":
            return []
        if kind == "groups":
            blocked_groups = blocked_groups or set()
            return [item["chat_id"] for item in items if item["chat_id"] not in blocked_groups]
        return [item["_id"] for item in items]

    tokens = selection.split(",")
    incremental = all(token.startswith(("+", "-")) for token in tokens)
    replacement = not any(token.startswith(("+", "-")) for token in tokens)
    if not incremental and not replacement:
        await _reply(update, "Use only plain indexes (1,3) or signed changes (+1,-2).")
        return None

    resolved = []
    for token in tokens:
        number = token[1:] if token.startswith(("+", "-")) else token
        if not number.isdigit():
            await _reply(update, f"Invalid {kind[:-1]} selection: {token}")
            return None
        index = int(number)
        if index < 1 or index > len(items):
            await _reply(update, f"Choose a {kind[:-1]} number between 1 and {len(items)}.")
            return None
        item = items[index - 1]
        if kind == "groups":
            chat_id = item["chat_id"]
            if chat_id in (blocked_groups or set()) and not token.startswith("-"):
                await _reply(update, "That group is inactive or blacklisted; reactivate/unblacklist it first.")
                return None
            resolved.append(chat_id)
        else:
            resolved.append(item["_id"])

    if replacement:
        return list(dict.fromkeys(resolved))
    result = list(selected)
    for token, value in zip(tokens, resolved):
        if token.startswith("-"):
            result = [existing for existing in result if existing != value]
        elif value not in result:
            result.append(value)
    return result


async def _edit_groups(update: Update, config: dict, words: list[str]) -> None:
    groups, blocked = await _groups_and_blocked()
    if not words:
        await _show_groups(update, config, groups, blocked)
        return
    selection = await _parse_selection(
        update,
        words,
        groups,
        config.get("group_ids", []),
        "groups",
        blocked_groups=blocked,
    )
    if selection is None:
        return
    updated = await autorotate.edit_config(
        str(config["_id"]),
        {"group_ids": selection},
    )
    if not updated:
        await _reply(update, "Pause or stop this configuration before editing its groups.")
        return
    await _reply(update, f"✅ Saved {len(selection)} authorized group(s) for {config['name']}.")


async def _edit_messages(update: Update, config: dict, words: list[str]) -> None:
    messages = await get_db().messages.find().sort("_id", 1).to_list(100)
    if not words:
        await _show_messages(update, config, messages)
        return
    selection = await _parse_selection(
        update, words, messages, config.get("message_ids", []), "messages"
    )
    if selection is None:
        return
    updated = await autorotate.edit_config(
        str(config["_id"]),
        {"message_ids": selection},
        reset_rotation=True,
    )
    if not updated:
        await _reply(update, "Pause or stop this configuration before editing its messages.")
        return
    await _reply(update, f"✅ Saved {len(selection)} message(s) in the selected rotation order.")


def _interval_text(config: dict) -> str:
    seconds = int(config.get("interval_seconds", 300))
    return (
        f"Current interval: {seconds} seconds.\n"
        "Options: 10, 20, 30 (private testing only), 1m, 2m, 5m, "
        "or a custom number of seconds (minimum 10; group posting requires at least 60)."
    )


async def _show_config(update: Update, config: dict) -> None:
    db = get_db()
    group_ids = config.get("group_ids", [])
    groups = await db.groups.find({"chat_id": {"$in": group_ids}}).to_list(len(group_ids))
    by_id = {group["chat_id"]: group for group in groups}
    group_lines = [
        f"• {_group_name(by_id[group_id])} ({group_id})" if group_id in by_id
        else f"• {group_id} (missing from syncgroups)"
        for group_id in group_ids
    ]
    messages = await db.messages.find({"_id": {"$in": config.get("message_ids", [])}}).to_list(100)
    message_by_id = {message["_id"]: message for message in messages}
    message_lines = [
        f"• {_preview(message_by_id[message_id])}" if message_id in message_by_id
        else f"• {message_id} (deleted)"
        for message_id in config.get("message_ids", [])
    ]
    status = config.get("status", "stopped")
    if config.get("test_mode") and status == "running":
        status += " (private test)"
    interval = int(config.get("interval_seconds", 300))
    text = (
        f"Auto-rotation: {config.get('name', 'Unnamed')}\n"
        f"ID: {config['_id']}\n"
        f"Status: {status}\n"
        f"Interval: {interval} seconds\n"
        f"Next message index: {int(config.get('next_message_index', 0)) + 1}\n\n"
        f"Groups ({len(group_lines)}):\n"
        + ("\n".join(group_lines) if group_lines else "• None selected")
        + "\n\n"
        + f"Messages ({len(message_lines)}):\n"
        + ("\n".join(message_lines) if message_lines else "• None selected")
    )
    if config.get("last_error"):
        text += f"\n\nLast error: {config['last_error']}"
    resume_after = config.get("resume_after")
    if isinstance(resume_after, datetime):
        if resume_after.tzinfo is None:
            resume_after = resume_after.replace(tzinfo=timezone.utc)
        text += f"\nResume available after: {resume_after.strftime('%Y-%m-%d %H:%M:%S UTC')}"
    await _reply(update, text)


async def _list_configs(update: Update) -> None:
    configs = await get_db().autorotate_configs.find().sort("created_at", 1).to_list(100)
    if not configs:
        await _reply(update, "No auto-rotation configurations. Use /autorotate create [name].")
        return
    all_group_ids = list({gid for config in configs for gid in config.get("group_ids", [])})
    group_docs = await get_db().groups.find(
        {"chat_id": {"$in": all_group_ids}}
    ).to_list(len(all_group_ids)) if all_group_ids else []
    group_by_id = {group["chat_id"]: group for group in group_docs}
    lines = ["Auto-rotation configurations"]
    for config in configs:
        group_names = [
            f"{_group_name(group_by_id[gid])} ({gid})" if gid in group_by_id else f"{gid} (missing)"
            for gid in config.get("group_ids", [])
        ]
        status = config.get("status", "stopped")
        if config.get("test_mode") and status == "running":
            status += " (private test)"
        lines.extend([
            "",
            f"{config.get('name', 'Unnamed')} — {status}",
            f"ID: {config['_id']} | Interval: {int(config.get('interval_seconds', 300))}s",
            "Groups: " + (", ".join(group_names) if group_names else "none"),
        ])
    await _reply(update, "\n".join(lines))


async def _show_logs(update: Update, config_id: str) -> None:
    try:
        parsed_id = autorotate.parse_config_id(config_id)
    except ValueError as exc:
        await _reply(update, str(exc))
        return
    events = await (
        get_db().promotion_logs.find(
            {"type": "autorotate", "config_id": str(parsed_id)}
        ).sort("_id", -1).limit(20).to_list(20)
    )
    if not events:
        await _reply(update, "No auto-rotation logs for this configuration.")
        return
    lines = [f"Last {len(events)} auto-rotation events for {parsed_id}"]
    for event in reversed(events):
        timestamp = event.get("timestamp")
        when = timestamp.strftime("%Y-%m-%d %H:%M:%S UTC") if timestamp else "?"
        action = event.get("action", "event")
        detail = event.get("error") or event.get("reason") or ""
        if event.get("chat_id") is not None:
            detail = f"group {event['chat_id']}" + (f": {detail}" if detail else "")
        if event.get("wait_seconds"):
            detail = f"wait {event['wait_seconds']}s" + (f": {detail}" if detail else "")
        lines.append(f"[{when}] {action}" + (f" — {detail}" if detail else ""))
    await _reply(update, "\n".join(lines))


@owner_only
async def cmd_autorotate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = list(context.args)
    if not args:
        await cmd_autorotate_menu(update, context)
        return

    command = args[0].lower()
    if command == "create":
        config = await autorotate.create_config(" ".join(args[1:]).strip())
        await _reply(
            update,
            f"✅ Created {config['name']}.\nID: {config['_id']}\n"
            f"Choose groups and messages, then set the interval before starting.",
        )
        return
    if command in {"list", "status"}:
        await _list_configs(update)
        return
    if command in {"help", "menu"}:
        await _reply(
            update,
            "Use /autorotate to open the menu.\n"
            "Create configs with /autorotate create [name], then select groups and messages.",
        )
        return
    if command == "logs":
        if len(args) < 2:
            await _reply(update, "Usage: /autorotate logs <configuration_id>")
            return
        await _show_logs(update, args[1])
        return
    if command in {"pause", "resume", "stop", "delete", "start", "test", "show", "groups", "messages", "interval"} and len(args) < 2:
        await _reply(update, f"Usage: /autorotate {command} <configuration_id>")
        return

    config = await _load_config(update, args[1])
    if not config:
        return
    config_id = str(config["_id"])

    if command == "show":
        await _show_config(update, config)
    elif command == "groups":
        await _edit_groups(update, config, args[2:])
    elif command == "messages":
        await _edit_messages(update, config, args[2:])
    elif command == "interval":
        if len(args) == 2:
            await _reply(update, _interval_text(config))
            return
        value = args[2].lower()
        seconds = autorotate.INTERVAL_PRESETS.get(value)
        if seconds is None:
            try:
                seconds = int(value)
            except ValueError:
                await _reply(update, "Use 10, 20, 30, 1m, 2m, 5m, or a custom interval in seconds.")
                return
        if seconds < 10:
            await _reply(update, "Intervals shorter than 10 seconds are not supported.")
            return
        updated = await autorotate.edit_config(config_id, {"interval_seconds": seconds})
        if not updated:
            await _reply(update, "Pause or stop this configuration before changing its interval.")
            return
        await _reply(update, f"✅ Interval set to {seconds} seconds. Group posting requires at least 60 seconds.")
    elif command in {"start", "test"}:
        success, result = await autorotate.start_config(
            config_id,
            context.bot,
            test_mode=(command == "test"),
        )
        await _reply(update, ("✅ " if success else "❌ ") + result)
    elif command == "pause":
        paused = await autorotate.pause_config(config_id)
        await _reply(update, "⏸ Configuration paused." if paused else "Configuration is not running.")
    elif command == "resume":
        success, result = await autorotate.resume_config(config_id, context.bot)
        await _reply(update, ("✅ " if success else "❌ ") + result)
    elif command == "stop":
        stopped = await autorotate.stop_config(config_id)
        await _reply(update, "🛑 Configuration stopped." if stopped else "Configuration not found.")
    elif command == "delete":
        deleted = await autorotate.delete_config(config_id)
        await _reply(update, "🗑 Configuration deleted." if deleted else "Configuration not found.")
    else:
        await _reply(update, "Unknown action. Use /autorotate to see available commands.")


def register(app):
    app.add_handler(CommandHandler("autorotate", cmd_autorotate))
    app.add_handler(CallbackQueryHandler(callback_autorotate, pattern=r"^ar:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_custom_interval))
