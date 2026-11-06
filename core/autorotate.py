import asyncio
import io
import logging
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from pyrogram.errors import FloodWait, SlowmodeWait

from config import OWNER_ID
from core.promotion import _log_event
from core.userbot import userbot
from database import get_db

log = logging.getLogger(__name__)

MIN_GROUP_INTERVAL_SECONDS = 5
MIN_CUSTOM_INTERVAL_SECONDS = 10
MAX_INTERVAL_SECONDS = 86400
INTERVAL_PRESETS = {
    "5": 5,
    "10": 10,
    "20": 20,
    "30": 30,
    "1m": 60,
    "2m": 120,
    "5m": 300,
}
INTERVAL_PRESET_LABELS = {
    5: "5 seconds",
    10: "10 seconds",
    20: "20 seconds",
    30: "30 seconds",
    60: "1 minute",
    120: "2 minutes",
    300: "5 minutes",
}

_jobs: dict[str, asyncio.Task] = {}
_recovery_jobs: dict[str, asyncio.Task] = {}
_group_send_lock = asyncio.Lock()
_media_cache: dict[str, bytes] = {}


def parse_config_id(value: str) -> ObjectId:
    if not ObjectId.is_valid(value):
        raise ValueError("Invalid configuration ID.")
    return ObjectId(value)


def next_message_index(message_ids: list, current_index: int, last_message_id=None) -> int:
    if not message_ids:
        raise ValueError("At least one saved message must be selected.")
    index = current_index % len(message_ids)
    if len(message_ids) > 1 and message_ids[index] == last_message_id:
        index = (index + 1) % len(message_ids)
    return index


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _config_key(config_id) -> str:
    return str(config_id)


def _collection():
    return get_db().autorotate_configs


def interval_validation_error(seconds: int) -> str | None:
    if seconds == MIN_GROUP_INTERVAL_SECONDS or MIN_CUSTOM_INTERVAL_SECONDS <= seconds <= MAX_INTERVAL_SECONDS:
        return None
    if seconds < MIN_CUSTOM_INTERVAL_SECONDS:
        return "Intervals below 10 seconds are unavailable; choose the 5-second preset or use at least 10 seconds."
    return f"Intervals cannot exceed {MAX_INTERVAL_SECONDS} seconds."


async def create_config(name: str) -> dict:
    now = _now()
    doc = {
        "name": name or f"Rotation {now.strftime('%Y-%m-%d %H:%M')}",
        "group_ids": [],
        "message_ids": [],
        "interval_seconds": 10,
        "next_message_index": 0,
        "last_message_id": None,
        "status": "stopped",
        "test_mode": False,
        "created_at": now,
        "updated_at": now,
    }
    result = await _collection().insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


async def get_config(config_id: str) -> dict | None:
    return await _collection().find_one({"_id": parse_config_id(config_id)})


async def edit_config(config_id: str, updates: dict, reset_rotation: bool = False) -> bool:
    object_id = parse_config_id(config_id)
    now = _now()
    fields = {**updates, "updated_at": now}
    if "interval_seconds" in updates:
        fields["next_run_at"] = now
    update = {"$set": fields}
    config = await _collection().find_one(
        {"_id": object_id, "status": {"$ne": "running"}},
        {
            "pending_message_id": 1,
            "pending_group_ids": 1,
            "pending_completed_group_ids": 1,
            "last_message_id": 1,
        },
    )
    if not config:
        return False
    if reset_rotation:
        fields.update({
            "next_message_index": 0,
            "last_message_id": (
                config.get("pending_message_id") or config.get("last_message_id")
            ),
            "pending_completed_group_ids": [],
        })
        update["$unset"] = {
            "pending_message_id": "",
            "pending_group_ids": "",
            "pending_message_index": "",
        }
    elif "group_ids" in updates and config.get("pending_message_id") is not None:
        next_group_ids = updates["group_ids"]
        fields["pending_group_ids"] = next_group_ids
        fields["pending_completed_group_ids"] = [
            chat_id
            for chat_id in config.get("pending_completed_group_ids", [])
            if chat_id in next_group_ids
        ]
    result = await _collection().update_one(
        {"_id": object_id, "status": {"$ne": "running"}},
        update,
    )
    return result.matched_count > 0


async def _write_log(config: dict, action: str, **fields) -> None:
    db = get_db()
    await _log_event(db, {
        "type": "autorotate",
        "config_id": str(config["_id"]),
        "config_name": config.get("name", ""),
        "action": action,
        **fields,
    })


async def _load_selected_messages(config: dict) -> list[dict]:
    message_ids = config.get("message_ids", [])
    if not message_ids:
        raise ValueError("No saved messages are selected.")
    docs = await get_db().messages.find({"_id": {"$in": message_ids}}).to_list(len(message_ids))
    by_id = {doc["_id"]: doc for doc in docs}
    missing = [str(message_id) for message_id in message_ids if message_id not in by_id]
    if missing:
        raise ValueError(f"Selected saved message(s) were deleted: {', '.join(missing)}")
    return [by_id[message_id] for message_id in message_ids]


async def _send_private_test(bot, message: dict) -> None:
    message_type = message.get("type")
    if message_type == "text":
        html = message.get("html")
        await bot.send_message(
            OWNER_ID,
            html or message["text"],
            parse_mode="HTML" if html else None,
        )
    elif message_type in {"photo", "video"}:
        caption_html = message.get("caption_html")
        caption = caption_html or message.get("caption") or None
        parse_mode = "HTML" if caption_html else None
        sender = bot.send_photo if message_type == "photo" else bot.send_video
        await sender(
            OWNER_ID,
            message["file_id"],
            caption=caption,
            parse_mode=parse_mode,
        )
    else:
        raise ValueError(f"Unsupported saved message type: {message_type!r}")


async def _download_media(bot, file_id: str) -> io.BytesIO:
    data = _media_cache.get(file_id)
    if data is None:
        telegram_file = await bot.get_file(file_id)
        buffer = io.BytesIO()
        await telegram_file.download_to_memory(buffer)
        data = buffer.getvalue()
        _media_cache[file_id] = data
        if len(_media_cache) > 5:
            _media_cache.pop(next(iter(_media_cache)))
    result = io.BytesIO(data)
    result.name = "autorotate-media"
    result.seek(0)
    return result


async def _wait_for_group_cooldown(db) -> None:
    now = _now()
    state_docs = await db.state.find(
        {"key": "autorotate_floodwait_until"}
    ).to_list(1)
    state_by_key = {doc["key"]: doc.get("value") for doc in state_docs}
    cooldown_until = _to_datetime(state_by_key.get("autorotate_floodwait_until"))
    if cooldown_until:
        delay = (cooldown_until - now).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)


async def _persist_floodwait_cooldown(wait_seconds: int, now: datetime | None = None) -> datetime:
    now = now or _now()
    resume_after = now + timedelta(seconds=max(0, int(wait_seconds)))
    await get_db().state.update_one(
        {"key": "autorotate_floodwait_until"},
        {"$set": {"value": resume_after, "updated_at": now}},
        upsert=True,
    )
    return resume_after


async def _send_group(bot, chat_id: int, message: dict) -> None:
    db = get_db()
    async with _group_send_lock:
        await _wait_for_group_cooldown(db)
        try:
            message_type = message.get("type")
            if message_type == "text":
                html = message.get("html")
                await userbot.send_message(
                    chat_id,
                    html or message["text"],
                    parse_mode="html" if html else None,
                )
            elif message_type in {"photo", "video"}:
                media = await _download_media(bot, message["file_id"])
                caption_html = message.get("caption_html")
                caption = caption_html or message.get("caption") or None
                parse_mode = "html" if caption_html else None
                sender = userbot.send_photo if message_type == "photo" else userbot.send_video
                await sender(
                    chat_id,
                    media,
                    caption=caption,
                    parse_mode=parse_mode,
                )
            else:
                raise ValueError(f"Unsupported saved message type: {message_type!r}")
        except FloodWait as exc:
            await _persist_floodwait_cooldown(int(exc.value))
            raise


async def _mark_group_complete(config_id: ObjectId, chat_id: int) -> None:
    await _collection().update_one(
        {"_id": config_id},
        {"$addToSet": {"pending_completed_group_ids": chat_id}},
    )


async def _pause_after_wait(config: dict, wait_seconds: int, reason: str, bot=None) -> None:
    now = _now()
    wait_seconds = max(0, int(wait_seconds))
    resume_after = now + timedelta(seconds=wait_seconds)
    fields = {
        "status": "paused",
        "resume_after": resume_after,
        "last_error": f"{reason}: Telegram requires waiting {wait_seconds} seconds.",
        "updated_at": now,
    }
    update = {"$set": fields}
    if reason == "FloodWait":
        fields["auto_resume"] = True
    else:
        update["$unset"] = {"auto_resume": ""}
    result = await _collection().update_one(
        {"_id": config["_id"], "status": "running"},
        update,
    )
    if reason == "FloodWait":
        await _persist_floodwait_cooldown(wait_seconds, now)
    await _write_log(
        config,
        "paused",
        reason=reason,
        wait_seconds=wait_seconds,
        resume_after=resume_after,
    )
    if reason == "FloodWait" and result.matched_count and bot is not None:
        config.update(fields)
        if _ensure_recovery_job(config, bot):
            log.info(
                "Auto-rotation %s FloodWait recovery scheduled for %s",
                config["_id"],
                resume_after.isoformat(),
            )
    log.warning(
        "Auto-rotation %s paused for %s (%ss); recovery at %s",
        config["_id"],
        reason,
        wait_seconds,
        resume_after.isoformat(),
    )


async def _pause_for_invalid_config(config: dict, error: str) -> None:
    await _collection().update_one(
        {"_id": config["_id"]},
        {"$set": {"status": "paused", "last_error": error, "updated_at": _now()},
         "$unset": {"auto_resume": ""}},
    )
    await _write_log(config, "paused", error=error)
    log.error("Auto-rotation %s paused: %s", config["_id"], error)


async def _finish_cycle(config: dict, message: dict, selected_index: int) -> int:
    next_index = (selected_index + 1) % len(config["message_ids"])
    now = _now()
    interval = int(config["interval_seconds"])
    next_run_at = _to_datetime(config.get("next_run_at")) or now
    next_run_at += timedelta(seconds=interval)
    if next_run_at <= now:
        missed_intervals = int((now - next_run_at).total_seconds() // interval) + 1
        next_run_at += timedelta(seconds=interval * missed_intervals)
    await _collection().update_one(
        {"_id": config["_id"]},
        {
            "$set": {
                "next_message_index": next_index,
                "last_message_id": message["_id"],
                "next_run_at": next_run_at,
                "updated_at": now,
            },
            "$unset": {
                "pending_message_id": "",
                "pending_group_ids": "",
                "pending_completed_group_ids": "",
                "pending_message_index": "",
            },
        },
    )
    return interval


async def _run_private_test(config: dict, bot, messages: list[dict]) -> int:
    selected_index = next_message_index(
        config["message_ids"],
        int(config.get("next_message_index", 0)),
        config.get("last_message_id"),
    )
    message = messages[selected_index]
    await _send_private_test(bot, message)
    await _write_log(config, "test_sent", message_id=str(message["_id"]), chat_id=OWNER_ID)
    log.info("Auto-rotation %s privately tested message %s", config["_id"], message["_id"])
    return await _finish_cycle(config, message, selected_index)


async def _run_group_cycle(config: dict, bot, messages: list[dict]) -> int:
    db = get_db()
    config_id = config["_id"]
    group_ids = list(config.get("group_ids", []))
    pending_message_id = config.get("pending_message_id")
    pending_groups = config.get("pending_group_ids")

    if pending_message_id is not None and pending_groups is not None:
        message_by_id = {message["_id"]: message for message in messages}
        message = message_by_id.get(pending_message_id)
        if message is None:
            await _pause_for_invalid_config(
                config,
                f"Pending saved message {pending_message_id} was deleted; edit the message selection.",
            )
            return 0
        message_index = int(config.get("pending_message_index", 0))
    else:
        message_index = next_message_index(
            config["message_ids"],
            int(config.get("next_message_index", 0)),
            config.get("last_message_id"),
        )
        message = messages[message_index]
        pending_groups = group_ids
        await _collection().update_one(
            {"_id": config_id},
            {"$set": {
                "pending_message_id": message["_id"],
                "pending_message_index": message_index,
                "pending_group_ids": pending_groups,
                "pending_completed_group_ids": [],
                "updated_at": _now(),
            }},
        )

    completed = set(config.get("pending_completed_group_ids", []))
    groups = await db.groups.find({"chat_id": {"$in": pending_groups}}).to_list(len(pending_groups))
    group_by_id = {group["chat_id"]: group for group in groups}
    inactive_docs = await db.inactive_groups.find(
        {"chat_id": {"$in": pending_groups}}, {"chat_id": 1}
    ).to_list(len(pending_groups))
    inactive = {doc["chat_id"] for doc in inactive_docs}
    blacklist_docs = await db.blacklist.find(
        {"chat_id": {"$in": pending_groups}}, {"chat_id": 1}
    ).to_list(len(pending_groups))
    blacklisted = {doc["chat_id"] for doc in blacklist_docs}

    for chat_id in pending_groups:
        if chat_id in completed:
            continue
        if chat_id not in group_by_id or chat_id in inactive or chat_id in blacklisted:
            error = "Group is no longer available, inactive, or blacklisted."
            await _write_log(config, "group_skipped", chat_id=chat_id, error=error)
            await _mark_group_complete(config_id, chat_id)
            continue

        try:
            await _send_group(bot, chat_id, message)
        except FloodWait as exc:
            await _pause_after_wait(config, int(exc.value), "FloodWait", bot)
            return 0
        except SlowmodeWait as exc:
            await _pause_after_wait(config, int(exc.value), "SlowmodeWait")
            return 0
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            await _write_log(config, "group_error", chat_id=chat_id, error=error)
            log.exception("Auto-rotation %s failed for group %s", config_id, chat_id)
            await _mark_group_complete(config_id, chat_id)
            continue

        await _write_log(
            config,
            "group_sent",
            chat_id=chat_id,
            group_title=group_by_id[chat_id].get("title", ""),
            message_id=str(message["_id"]),
        )
        log.info("Auto-rotation %s sent message to group %s", config_id, chat_id)
        await _mark_group_complete(config_id, chat_id)

    interval = await _finish_cycle(config, message, message_index)
    return interval


async def _run_config(config_id: ObjectId, bot) -> None:
    key = _config_key(config_id)
    try:
        while True:
            config = await _collection().find_one({"_id": config_id})
            if not config or config.get("status") != "running":
                return

            next_run_at = _to_datetime(config.get("next_run_at"))
            if next_run_at:
                delay = (next_run_at - _now()).total_seconds()
                if delay > 0:
                    await asyncio.sleep(delay)

            config = await _collection().find_one({"_id": config_id})
            if not config or config.get("status") != "running":
                return

            try:
                messages = await _load_selected_messages(config)
                if config.get("test_mode"):
                    await _run_private_test(config, bot, messages)
                else:
                    await _run_group_cycle(config, bot, messages)
            except ValueError as exc:
                await _pause_for_invalid_config(config, str(exc))
                return

            refreshed = await _collection().find_one({"_id": config_id}, {"status": 1})
            if not refreshed or refreshed.get("status") != "running":
                return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        config = await _collection().find_one({"_id": config_id})
        if config:
            await _pause_for_invalid_config(config, f"{type(exc).__name__}: {exc}")
        log.exception("Auto-rotation task %s failed", key)
    finally:
        if _jobs.get(key) is asyncio.current_task():
            _jobs.pop(key, None)


def _ensure_job(config: dict, bot) -> bool:
    key = _config_key(config["_id"])
    existing = _jobs.get(key)
    if existing and not existing.done():
        return False
    _jobs[key] = asyncio.create_task(
        _run_config(config["_id"], bot),
        name=f"autorotate-{key}",
    )
    return True


async def _cancel_job(config_id: ObjectId) -> None:
    task = _jobs.pop(_config_key(config_id), None)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _cancel_recovery_job(config_id: ObjectId) -> None:
    task = _recovery_jobs.pop(_config_key(config_id), None)
    if task and not task.done() and task is not asyncio.current_task():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _recover_after_floodwait(config_id: ObjectId, bot) -> None:
    key = _config_key(config_id)
    try:
        while True:
            config = await _collection().find_one({"_id": config_id})
            if (
                not config
                or config.get("status") != "paused"
                or not config.get("auto_resume")
            ):
                log.info(
                    "Auto-rotation %s FloodWait recovery cancelled; configuration is no longer eligible",
                    key,
                )
                return

            resume_after = _to_datetime(config.get("resume_after"))
            if not resume_after:
                log.error("Auto-rotation %s FloodWait recovery has no valid resume time", key)
                return
            delay = (resume_after - _now()).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)
                continue

            # A paused config's previous scheduler task may still be unwinding.
            previous_job = _jobs.get(key)
            if previous_job and not previous_job.done() and previous_job is not asyncio.current_task():
                await asyncio.gather(previous_job, return_exceptions=True)

            config = await _collection().find_one({"_id": config_id})
            if (
                not config
                or config.get("status") != "paused"
                or not config.get("auto_resume")
                or _to_datetime(config.get("resume_after")) != resume_after
            ):
                log.info("Auto-rotation %s FloodWait recovery cancelled before resume", key)
                return

            error = await _validate_for_start(config, bool(config.get("test_mode")))
            if error:
                await _collection().update_one(
                    {
                        "_id": config_id,
                        "status": "paused",
                        "auto_resume": True,
                        "resume_after": resume_after,
                    },
                    {"$set": {"last_error": error, "updated_at": _now()},
                     "$unset": {"auto_resume": ""}},
                )
                await _write_log(config, "recovery_failed", reason="FloodWait", error=error)
                log.error("Auto-rotation %s FloodWait recovery not resumed: %s", key, error)
                return

            if await _is_duplicate(config):
                error = "An identical rotation configuration already exists."
                await _collection().update_one(
                    {
                        "_id": config_id,
                        "status": "paused",
                        "auto_resume": True,
                        "resume_after": resume_after,
                    },
                    {"$set": {"last_error": error, "updated_at": _now()},
                     "$unset": {"auto_resume": ""}},
                )
                await _write_log(config, "recovery_failed", reason="FloodWait", error=error)
                log.error("Auto-rotation %s FloodWait recovery not resumed: %s", key, error)
                return

            result = await _collection().update_one(
                {
                    "_id": config_id,
                    "status": "paused",
                    "auto_resume": True,
                    "resume_after": resume_after,
                },
                {"$set": {
                    "status": "running",
                    "updated_at": _now(),
                    "last_error": None,
                }, "$unset": {"auto_resume": "", "resume_after": ""}},
            )
            if not result.matched_count:
                log.info("Auto-rotation %s FloodWait recovery skipped; configuration changed", key)
                return

            config.update({"status": "running", "auto_resume": False})
            if not _ensure_job(config, bot):
                log.info("Auto-rotation %s FloodWait recovery continued its existing scheduler job", key)
            current = await _collection().find_one({"_id": config_id}, {"status": 1})
            if current and current.get("status") != "running":
                await _cancel_job(config_id)
                log.info("Auto-rotation %s remained paused after FloodWait recovery", key)
                return

            await _write_log(config, "resumed", reason="FloodWait", resume_after=resume_after)
            log.info("Auto-rotation %s resumed after FloodWait at %s", key, _now().isoformat())
            return
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Auto-rotation %s FloodWait recovery failed", key)
    finally:
        if _recovery_jobs.get(key) is asyncio.current_task():
            _recovery_jobs.pop(key, None)


def _ensure_recovery_job(config: dict, bot) -> bool:
    key = _config_key(config["_id"])
    existing = _recovery_jobs.get(key)
    if existing and not existing.done():
        return False
    _recovery_jobs[key] = asyncio.create_task(
        _recover_after_floodwait(config["_id"], bot),
        name=f"autorotate-recovery-{key}",
    )
    return True


async def _validate_for_start(config: dict, test_mode: bool) -> str | None:
    if not config.get("message_ids"):
        return "Select at least one saved message before starting."
    try:
        await _load_selected_messages(config)
    except ValueError as exc:
        return str(exc)
    interval = int(config.get("interval_seconds", 0))
    interval_error = interval_validation_error(interval)
    if interval_error:
        if test_mode:
            return f"Private test interval is invalid. {interval_error}"
        return f"Group posting requires at least 5 seconds. {interval_error}"
    if test_mode:
        return None
    group_ids = config.get("group_ids", [])
    if not group_ids:
        return "Select at least one authorized group before starting."

    db = get_db()
    groups = await db.groups.find({"chat_id": {"$in": group_ids}}, {"chat_id": 1}).to_list(len(group_ids))
    known_ids = {group["chat_id"] for group in groups}
    missing = [chat_id for chat_id in group_ids if chat_id not in known_ids]
    if missing:
        return f"Group(s) no longer exist in syncgroups: {', '.join(map(str, missing))}."
    inactive_docs = await db.inactive_groups.find(
        {"chat_id": {"$in": group_ids}}, {"chat_id": 1}
    ).to_list(len(group_ids))
    if inactive_docs:
        return "Remove or reactivate inactive group(s) before starting."
    blacklist_docs = await db.blacklist.find(
        {"chat_id": {"$in": group_ids}}, {"chat_id": 1}
    ).to_list(len(group_ids))
    if blacklist_docs:
        return "Remove blacklisted group(s) from the selection before starting."
    return None


async def _is_duplicate(config: dict) -> bool:
    candidates = await _collection().find({"_id": {"$ne": config["_id"]}}).to_list(None)
    current_signature = _duplicate_signature(config)
    for candidate in candidates:
        if _duplicate_signature(candidate) == current_signature:
            return True
    return False


def _duplicate_signature(config: dict) -> tuple:
    test_mode = bool(config.get("test_mode"))
    groups = frozenset() if test_mode else frozenset(config.get("group_ids", []))
    return (
        groups,
        tuple(config.get("message_ids", [])),
        int(config.get("interval_seconds", 0)),
        test_mode,
    )


async def start_config(config_id: str, bot, test_mode: bool = False) -> tuple[bool, str]:
    object_id = parse_config_id(config_id)
    config = await _collection().find_one({"_id": object_id})
    if not config:
        return False, "Configuration not found."
    if config.get("status") == "running":
        return False, "Configuration is already running."
    resume_after = _to_datetime(config.get("resume_after"))
    if resume_after and resume_after > _now():
        return False, f"Telegram cooldown is active until {resume_after.strftime('%Y-%m-%d %H:%M:%S UTC')}."
    error = await _validate_for_start(config, test_mode)
    if error:
        return False, error
    mode_changed = bool(config.get("test_mode")) != test_mode
    config["test_mode"] = test_mode
    if await _is_duplicate(config):
        return False, "An identical rotation configuration already exists."

    updates = {
        "status": "running",
        "test_mode": test_mode,
        "updated_at": _now(),
        "last_error": None,
    }
    has_pending_cycle = (
        config.get("pending_message_id") is not None
        and config.get("pending_group_ids") is not None
    )
    if mode_changed:
        updates.update({
            "pending_message_id": None,
            "pending_group_ids": None,
            "pending_completed_group_ids": [],
        })
    if mode_changed or not has_pending_cycle:
        updates["next_run_at"] = _now()
    await _collection().update_one(
        {"_id": object_id},
        {"$set": updates, "$unset": {"resume_after": "", "auto_resume": ""}},
    )
    await _cancel_recovery_job(object_id)
    config.update(updates)
    if not _ensure_job(config, bot):
        return False, "A scheduler job for this configuration is already running."
    await _write_log(config, "started", test_mode=test_mode)
    return True, "Private test started." if test_mode else "Group rotation started."


async def pause_config(config_id: str) -> bool:
    object_id = parse_config_id(config_id)
    result = await _collection().update_one(
        {"_id": object_id, "status": "running"},
        {"$set": {"status": "paused", "updated_at": _now()},
         "$unset": {"auto_resume": ""}},
    )
    if result.modified_count:
        await _cancel_job(object_id)
        await _cancel_recovery_job(object_id)
        config = await _collection().find_one({"_id": object_id})
        if config:
            await _write_log(config, "paused")
        return True
    result = await _collection().update_one(
        {"_id": object_id, "status": "paused", "auto_resume": True},
        {"$set": {"updated_at": _now()}, "$unset": {"auto_resume": ""}},
    )
    if result.modified_count:
        await _cancel_recovery_job(object_id)
        config = await _collection().find_one({"_id": object_id})
        if config:
            await _write_log(config, "paused", reason="manual")
        log.info("Auto-rotation %s FloodWait recovery cancelled by manual pause", object_id)
        return True
    return False


async def resume_config(config_id: str, bot) -> tuple[bool, str]:
    object_id = parse_config_id(config_id)
    config = await _collection().find_one({"_id": object_id})
    if not config:
        return False, "Configuration not found."
    if config.get("status") != "paused":
        return False, "Configuration is not paused."
    resume_after = _to_datetime(config.get("resume_after"))
    if resume_after and resume_after > _now():
        return False, f"Telegram cooldown is active until {resume_after.strftime('%Y-%m-%d %H:%M:%S UTC')}."
    error = await _validate_for_start(config, bool(config.get("test_mode")))
    if error:
        return False, error
    config["status"] = "running"
    if await _is_duplicate(config):
        return False, "An identical rotation configuration already exists."
    await _collection().update_one(
        {"_id": object_id},
        {"$set": {"status": "running", "updated_at": _now(), "last_error": None},
         "$unset": {"resume_after": "", "auto_resume": ""}},
    )
    await _cancel_recovery_job(object_id)
    if not _ensure_job(config, bot):
        await _collection().update_one(
            {"_id": object_id},
            {"$set": {"status": "paused", "updated_at": _now()}},
        )
        return False, "A scheduler job for this configuration is already running."
    await _write_log(config, "resumed", test_mode=bool(config.get("test_mode")))
    return True, "Configuration resumed."


async def stop_config(config_id: str) -> bool:
    object_id = parse_config_id(config_id)
    config = await _collection().find_one({"_id": object_id})
    if not config:
        return False
    await _collection().update_one(
        {"_id": object_id},
        {"$set": {
            "status": "stopped",
            "updated_at": _now(),
        }, "$unset": {"auto_resume": ""}},
        upsert=False,
    )
    await _cancel_job(object_id)
    await _cancel_recovery_job(object_id)
    await _write_log(config, "stopped")
    return True


async def delete_config(config_id: str) -> bool:
    object_id = parse_config_id(config_id)
    await _cancel_job(object_id)
    await _cancel_recovery_job(object_id)
    result = await _collection().delete_one({"_id": object_id})
    return result.deleted_count > 0


async def restore_running_configs(bot) -> int:
    configs = await _collection().find({"status": "running"}).sort("_id", 1).to_list(None)
    restored = 0
    seen_signatures = set()
    for config in configs:
        test_mode = bool(config.get("test_mode"))
        error = await _validate_for_start(config, test_mode)
        if error:
            await _pause_for_invalid_config(config, error)
            continue
        signature = _duplicate_signature(config)
        if signature in seen_signatures:
            await _pause_for_invalid_config(config, "Duplicate running configuration; another identical configuration was restored.")
            continue
        seen_signatures.add(signature)
        if _ensure_job(config, bot):
            restored += 1
            log.info("Restored auto-rotation configuration %s", config["_id"])
    pending_recoveries = await _collection().find(
        {"status": "paused", "auto_resume": True}
    ).to_list(None)
    for config in pending_recoveries:
        if not _to_datetime(config.get("resume_after")):
            log.error(
                "Auto-rotation %s has FloodWait recovery enabled without a valid resume time",
                config["_id"],
            )
            continue
        if _ensure_recovery_job(config, bot):
            log.info(
                "Restored Auto-rotation %s FloodWait recovery for %s",
                config["_id"],
                _to_datetime(config["resume_after"]).isoformat(),
            )
    return restored


async def shutdown() -> None:
    tasks = list(_jobs.values()) + list(_recovery_jobs.values())
    _jobs.clear()
    _recovery_jobs.clear()
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def active_job_count() -> int:
    return sum(not task.done() for task in _jobs.values())
