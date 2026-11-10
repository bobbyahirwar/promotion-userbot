import asyncio
import base64
import copy
import io
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    asyncio.get_running_loop()
    _IMPORT_LOOP = None
except RuntimeError:
    _IMPORT_LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(_IMPORT_LOOP)

os.environ.update({
    "BOT_TOKEN": "123:validation-token",
    "API_ID": "1",
    "API_HASH": "validation-hash",
    "SESSION_STRING": base64.urlsafe_b64encode(b"x" * 271).decode().rstrip("="),
    "OWNER_ID": "1",
    "MONGO_URI": "mongodb://127.0.0.1:27017/promotionbot",
})

from bson import ObjectId

from core import autorotate
from handlers.autorotate import _parse_selection

if _IMPORT_LOOP:
    _IMPORT_LOOP.close()
    asyncio.set_event_loop(None)


def _matches(document: dict, query: dict) -> bool:
    for key, expected in query.items():
        actual = document.get(key)
        if isinstance(expected, dict):
            if "$in" in expected and actual not in expected["$in"]:
                return False
            if "$ne" in expected and actual == expected["$ne"]:
                return False
        elif actual != expected:
            return False
    return True


class FakeCursor:
    def __init__(self, documents):
        self.documents = documents

    def sort(self, key, direction=1):
        self.documents.sort(key=lambda doc: doc.get(key), reverse=direction < 0)
        return self

    def limit(self, count):
        self.documents = self.documents[:count]
        return self

    async def to_list(self, length):
        selected = self.documents if length is None else self.documents[:length]
        return copy.deepcopy(selected)


class FakeCollection:
    def __init__(self):
        self.documents = []

    async def insert_one(self, document):
        stored = copy.deepcopy(document)
        stored.setdefault("_id", ObjectId())
        self.documents.append(stored)
        return SimpleNamespace(inserted_id=stored["_id"])

    async def find_one(self, query=None, projection=None):
        query = query or {}
        for document in self.documents:
            if _matches(document, query):
                return copy.deepcopy(document)
        return None

    def find(self, query=None, projection=None):
        query = query or {}
        selected = [copy.deepcopy(doc) for doc in self.documents if _matches(doc, query)]
        return FakeCursor(selected)

    async def update_one(self, query, update, upsert=False):
        for document in self.documents:
            if not _matches(document, query):
                continue
            before = copy.deepcopy(document)
            for key, value in update.get("$set", {}).items():
                document[key] = copy.deepcopy(value)
            for key in update.get("$unset", {}):
                document.pop(key, None)
            for key, value in update.get("$addToSet", {}).items():
                values = document.setdefault(key, [])
                if value not in values:
                    values.append(value)
            return SimpleNamespace(
                matched_count=1,
                modified_count=int(document != before),
                upserted_id=None,
            )
        if upsert:
            document = {
                key: value
                for key, value in query.items()
                if not isinstance(value, dict)
            }
            document.update(copy.deepcopy(update.get("$set", {})))
            for key, value in update.get("$addToSet", {}).items():
                document[key] = [value]
            self.documents.append(document)
            return SimpleNamespace(
                matched_count=0,
                modified_count=0,
                upserted_id=document.get("_id"),
            )
        return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=None)

    async def delete_one(self, query):
        for index, document in enumerate(self.documents):
            if _matches(document, query):
                del self.documents[index]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    async def delete_many(self, query):
        before = len(self.documents)
        self.documents = [doc for doc in self.documents if not _matches(doc, query)]
        return SimpleNamespace(deleted_count=before - len(self.documents))

    async def count_documents(self, query=None):
        return sum(_matches(doc, query or {}) for doc in self.documents)


class FakeDatabase:
    def __init__(self):
        for name in (
            "autorotate_configs",
            "messages",
            "groups",
            "inactive_groups",
            "blacklist",
            "state",
            "promotion_logs",
        ):
            setattr(self, name, FakeCollection())


class AutoRotateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = FakeDatabase()
        self.database_patch = patch.object(autorotate, "get_db", return_value=self.db)
        self.database_patch.start()
        self.log_patch = patch.object(autorotate, "_log_event", new=self._log_event)
        self.log_patch.start()
        await autorotate.shutdown()
        autorotate._media_cache.clear()

    async def asyncTearDown(self):
        await autorotate.shutdown()
        self.log_patch.stop()
        self.database_patch.stop()

    async def _log_event(self, db, event):
        await db.promotion_logs.insert_one(event)

    async def _new_valid_config(self, name="rotation", chat_id=101, interval=60):
        message = {"_id": ObjectId(), "type": "text", "text": "saved"}
        await self.db.messages.insert_one(message)
        await self.db.groups.insert_one({"chat_id": chat_id, "title": f"Group {chat_id}"})
        config = await autorotate.create_config(name)
        await autorotate.edit_config(
            str(config["_id"]),
            {
                "group_ids": [chat_id],
                "message_ids": [message["_id"]],
                "interval_seconds": interval,
            },
            reset_rotation=True,
        )
        return await autorotate.get_config(str(config["_id"]))

    async def test_new_config_defaults_to_ten_second_interval(self):
        config = await autorotate.create_config("default-interval")

        self.assertEqual(config["interval_seconds"], 10)

    def test_rotation_advances_in_selection_order_without_consecutive_repeat(self):
        selected = ["first", "second", "third"]
        self.assertEqual(autorotate.next_message_index(selected, 0), 0)
        self.assertEqual(autorotate.next_message_index(selected, 1, "second"), 2)
        self.assertEqual(autorotate.next_message_index(selected, 0, "first"), 1)
        self.assertEqual(autorotate.next_message_index(["only"], 0, "only"), 0)

    def test_private_test_duplicate_detection_ignores_group_selection(self):
        message_id = ObjectId()
        first = {
            "group_ids": [101],
            "message_ids": [message_id],
            "interval_seconds": 10,
            "test_mode": True,
        }
        second = {**first, "group_ids": [202]}
        self.assertEqual(
            autorotate._duplicate_signature(first),
            autorotate._duplicate_signature(second),
        )

    async def test_message_selection_supports_all_none_and_incremental_edits(self):
        messages = [{"_id": ObjectId()} for _ in range(3)]
        first, second, third = [message["_id"] for message in messages]
        added_removed = await _parse_selection(
            None, ["+3,-1"], messages, [first, second], "messages"
        )
        self.assertEqual(added_removed, [second, third])
        self.assertEqual(
            await _parse_selection(None, ["all"], messages, [], "messages"),
            [first, second, third],
        )
        self.assertEqual(
            await _parse_selection(None, ["none"], messages, [first], "messages"),
            [],
        )

    async def test_selected_saved_messages_load_from_storage_in_configured_order(self):
        first = {
            "_id": ObjectId(),
            "type": "photo",
            "file_id": "photo-id",
            "caption": "first caption",
            "caption_html": "<b>first caption</b>",
        }
        second = {
            "_id": ObjectId(),
            "type": "video",
            "file_id": "video-id",
            "caption": "second caption",
        }
        await self.db.messages.insert_one(first)
        await self.db.messages.insert_one(second)

        selected = await autorotate._load_selected_messages({
            "message_ids": [second["_id"], first["_id"]],
        })

        self.assertEqual([message["_id"] for message in selected], [second["_id"], first["_id"]])
        self.assertEqual(selected[0]["caption"], "second caption")
        self.assertEqual(selected[0]["file_id"], "video-id")
        self.assertEqual(selected[1]["caption_html"], "<b>first caption</b>")

    async def test_group_selection_selects_all_and_supports_add_remove(self):
        groups = [{"chat_id": 101}, {"chat_id": 202}, {"chat_id": 303}]
        selected_all = await _parse_selection(
            None, ["all"], groups, [], "groups", blocked_groups={202}
        )
        self.assertEqual(selected_all, [101, 303])
        selected = await _parse_selection(
            None, ["+3,-1"], groups, [101, 202], "groups", blocked_groups={202}
        )
        self.assertEqual(selected, [202, 303])

    async def test_private_rotation_sends_in_selected_order_and_persists_cursor(self):
        first = {"_id": ObjectId(), "type": "text", "text": "first"}
        second = {"_id": ObjectId(), "type": "text", "text": "second"}
        await self.db.messages.insert_one(first)
        await self.db.messages.insert_one(second)
        config = await autorotate.create_config("private-test")
        await autorotate.edit_config(
            str(config["_id"]),
            {
                "message_ids": [first["_id"], second["_id"]],
                "interval_seconds": 10,
                "test_mode": True,
            },
            reset_rotation=True,
        )
        config = await autorotate.get_config(str(config["_id"]))

        class FakeBot:
            def __init__(self):
                self.sent_text = []

            async def send_message(self, chat_id, text, **kwargs):
                self.sent_text.append((chat_id, text))

        bot = FakeBot()
        selected = [first, second]
        await autorotate._run_private_test(config, bot, selected)
        config = await autorotate.get_config(str(config["_id"]))
        await autorotate._run_private_test(config, bot, selected)
        self.assertEqual([text for _, text in bot.sent_text], ["first", "second"])
        persisted = await autorotate.get_config(str(config["_id"]))
        self.assertEqual(persisted["next_message_index"], 0)
        self.assertEqual(persisted["last_message_id"], second["_id"])

    async def test_group_rotation_sends_each_selected_message_in_order(self):
        first = {"_id": ObjectId(), "type": "text", "text": "first"}
        second = {"_id": ObjectId(), "type": "text", "text": "second"}
        await self.db.messages.insert_one(first)
        await self.db.messages.insert_one(second)
        await self.db.groups.insert_one({"chat_id": 101, "title": "One"})
        await self.db.groups.insert_one({"chat_id": 202, "title": "Two"})
        config = await autorotate.create_config("group-order")
        await autorotate.edit_config(
            str(config["_id"]),
            {
                "group_ids": [101, 202],
                "message_ids": [first["_id"], second["_id"]],
                "interval_seconds": 10,
            },
            reset_rotation=True,
        )
        sent = []

        async def record_send(bot, chat_id, message):
            sent.append((chat_id, message["_id"]))

        with patch.object(autorotate, "_send_group", new=record_send):
            config = await autorotate.get_config(str(config["_id"]))
            await autorotate._run_group_cycle(config, object(), [first, second])
            config = await autorotate.get_config(str(config["_id"]))
            await autorotate._run_group_cycle(config, object(), [first, second])

        self.assertEqual(
            sent,
            [
                (101, first["_id"]),
                (202, first["_id"]),
                (101, second["_id"]),
                (202, second["_id"]),
            ],
        )

    async def test_group_delivery_failure_does_not_skip_other_selected_groups(self):
        message = {"_id": ObjectId(), "type": "text", "text": "saved"}
        await self.db.messages.insert_one(message)
        await self.db.groups.insert_one({"chat_id": 101, "title": "One"})
        await self.db.groups.insert_one({"chat_id": 202, "title": "Two"})
        config = await autorotate.create_config("independent-failures")
        await autorotate.edit_config(
            str(config["_id"]),
            {
                "group_ids": [101, 202],
                "message_ids": [message["_id"]],
                "interval_seconds": 10,
            },
            reset_rotation=True,
        )
        attempted = []

        async def send_with_failure(bot, chat_id, saved_message):
            attempted.append(chat_id)
            if chat_id == 101:
                raise RuntimeError("group unavailable")

        with patch.object(autorotate, "_send_group", new=send_with_failure):
            config = await autorotate.get_config(str(config["_id"]))
            await autorotate._run_group_cycle(config, object(), [message])

        persisted = await autorotate.get_config(str(config["_id"]))
        self.assertEqual(attempted, [101, 202])
        self.assertEqual(persisted["next_message_index"], 0)
        self.assertEqual(persisted["last_message_id"], message["_id"])
        self.assertEqual(persisted["group_ids"], [101, 202])

    async def test_permanent_send_errors_remove_only_the_unavailable_groups(self):
        message = {"_id": ObjectId(), "type": "text", "text": "saved"}
        await self.db.messages.insert_one(message)
        for chat_id in (101, 202, 303):
            await self.db.groups.insert_one({"chat_id": chat_id, "title": f"Group {chat_id}"})
        config = await autorotate.create_config("remove-unavailable")
        await autorotate.edit_config(
            str(config["_id"]),
            {"group_ids": [101, 202, 303], "message_ids": [message["_id"]]},
            reset_rotation=True,
        )
        await self.db.autorotate_configs.update_one(
            {"_id": config["_id"]},
            {"$set": {"status": "running"}},
        )
        errors = {
            101: autorotate.ChatWriteForbidden(400, "CHAT_WRITE_FORBIDDEN"),
            202: autorotate.ChannelPrivate(400, "CHANNEL_PRIVATE"),
        }
        attempted = []

        async def send_with_permanent_errors(bot, chat_id, saved_message):
            attempted.append(chat_id)
            if chat_id in errors:
                raise errors[chat_id]

        with patch.object(autorotate, "_send_group", new=send_with_permanent_errors):
            config = await autorotate.get_config(str(config["_id"]))
            await autorotate._run_group_cycle(config, object(), [message])

        persisted = await autorotate.get_config(str(config["_id"]))
        removed_logs = await self.db.promotion_logs.find(
            {"action": "group_removed"}
        ).to_list(None)
        self.assertEqual(attempted, [101, 202, 303])
        self.assertEqual(persisted["group_ids"], [303])
        self.assertEqual(persisted["status"], "running")
        self.assertEqual(await self.db.groups.count_documents({}), 3)
        self.assertEqual({event["chat_id"] for event in removed_logs}, {101, 202})

    async def test_temporary_send_error_does_not_remove_group(self):
        config = await self._new_valid_config(chat_id=101)
        config["status"] = "running"
        message = {"_id": config["message_ids"][0], "type": "text", "text": "saved"}

        async def temporary_failure(bot, chat_id, saved_message):
            raise TimeoutError("temporary network issue")

        with patch.object(autorotate, "_send_group", new=temporary_failure):
            await autorotate._run_group_cycle(config, object(), [message])

        persisted = await autorotate.get_config(str(config["_id"]))
        self.assertEqual(persisted["group_ids"], [101])

    async def test_group_cooldown_uses_floodwait_not_old_global_send_gap(self):
        await self.db.state.insert_one({
            "key": "autorotate_last_group_post_at",
            "value": autorotate._now(),
        })
        sleep = AsyncMock()

        with patch.object(autorotate.asyncio, "sleep", new=sleep):
            await autorotate._wait_for_group_cooldown(self.db)
        sleep.assert_not_awaited()

        await self.db.state.insert_one({
            "key": "autorotate_floodwait_until",
            "value": autorotate._now() + autorotate.timedelta(seconds=30),
        })
        with patch.object(autorotate.asyncio, "sleep", new=sleep):
            await autorotate._wait_for_group_cooldown(self.db)
        sleep.assert_awaited_once()

    async def test_cycle_deadlines_remain_aligned_to_scheduled_interval(self):
        config = await self._new_valid_config(interval=10)
        scheduled_at = autorotate._now() - autorotate.timedelta(seconds=24)
        await self.db.autorotate_configs.update_one(
            {"_id": config["_id"]},
            {"$set": {"next_run_at": scheduled_at}},
        )
        config = await autorotate.get_config(str(config["_id"]))
        message = {"_id": config["message_ids"][0]}

        interval = await autorotate._finish_cycle(config, message, 0)

        persisted = await autorotate.get_config(str(config["_id"]))
        next_run_at = persisted["next_run_at"]
        elapsed = (next_run_at - scheduled_at).total_seconds()
        self.assertEqual(interval, 10)
        self.assertGreater(next_run_at, autorotate._now())
        self.assertAlmostEqual(elapsed / interval, round(elapsed / interval), places=5)

    async def test_group_sender_preserves_text_media_and_html_captions(self):
        class FakeUserbot:
            def __init__(self):
                self.sent = []

            async def send_message(self, chat_id, text, **kwargs):
                self.sent.append(("text", chat_id, text, kwargs))

            async def send_photo(self, chat_id, media, **kwargs):
                self.sent.append(("photo", chat_id, media, kwargs))

            async def send_video(self, chat_id, media, **kwargs):
                self.sent.append(("video", chat_id, media, kwargs))

        fake_userbot = FakeUserbot()
        messages = [
            {"type": "text", "text": "plain", "html": "<b>plain</b>"},
            {
                "type": "photo",
                "file_id": "photo-id",
                "caption": "caption",
                "caption_html": "<i>caption</i>",
            },
            {
                "type": "video",
                "file_id": "video-id",
                "caption": "video caption",
            },
        ]

        async def fake_download(bot, file_id):
            return io.BytesIO(file_id.encode())

        with patch.object(autorotate, "userbot", fake_userbot), patch.object(
            autorotate, "_download_media", new=fake_download
        ), patch.object(autorotate, "_wait_for_group_cooldown", new=lambda db: asyncio.sleep(0)):
            for message in messages:
                await autorotate._send_group(object(), 101, message)

        self.assertEqual([entry[0] for entry in fake_userbot.sent], ["text", "photo", "video"])
        self.assertEqual(fake_userbot.sent[0][2], "<b>plain</b>")
        self.assertEqual(fake_userbot.sent[0][3]["parse_mode"], "html")
        self.assertEqual(fake_userbot.sent[1][2].getvalue(), b"photo-id")
        self.assertEqual(fake_userbot.sent[1][3]["caption"], "<i>caption</i>")
        self.assertEqual(fake_userbot.sent[1][3]["parse_mode"], "html")
        self.assertEqual(fake_userbot.sent[2][2].getvalue(), b"video-id")
        self.assertEqual(fake_userbot.sent[2][3]["caption"], "video caption")

    async def test_floodwait_resumes_pending_groups_without_duplicate_success(self):
        first = {"_id": ObjectId(), "type": "text", "text": "first"}
        second = {"_id": ObjectId(), "type": "text", "text": "second"}
        await self.db.messages.insert_one(first)
        await self.db.messages.insert_one(second)
        await self.db.groups.insert_one({"chat_id": 101, "title": "One"})
        await self.db.groups.insert_one({"chat_id": 202, "title": "Two"})
        config = await autorotate.create_config("flood-resume")
        await autorotate.edit_config(
            str(config["_id"]),
            {"group_ids": [101, 202], "message_ids": [first["_id"], second["_id"]]},
            reset_rotation=True,
        )
        await self.db.autorotate_configs.update_one(
            {"_id": config["_id"]},
            {"$set": {
                "status": "running",
                "next_message_index": 1,
                "last_message_id": first["_id"],
            }},
        )
        attempted = []
        current_time = [autorotate._now()]
        original_sleep = asyncio.sleep

        class FakeFloodWait(Exception):
            def __init__(self, value):
                self.value = value

        async def send_with_wait(bot, chat_id, message):
            attempted.append(chat_id)
            if chat_id == 202 and attempted.count(202) == 1:
                raise FakeFloodWait(30)

        async def advance_time(delay):
            current_time[0] += autorotate.timedelta(seconds=delay)
            await original_sleep(0)

        async def run_resumed_cycle(config_id, bot):
            resumed_config = await autorotate.get_config(str(config_id))
            messages = await autorotate._load_selected_messages(resumed_config)
            await autorotate._run_group_cycle(resumed_config, bot, messages)
            await self.db.autorotate_configs.update_one(
                {"_id": config_id},
                {"$set": {"status": "stopped"}},
            )

        with (
            patch.object(autorotate, "FloodWait", FakeFloodWait),
            patch.object(autorotate, "_send_group", new=send_with_wait),
            patch.object(autorotate, "_now", side_effect=lambda: current_time[0]),
            patch.object(autorotate.asyncio, "sleep", new=advance_time),
            patch.object(autorotate, "_run_config", new=run_resumed_cycle),
        ):
            config = await autorotate.get_config(str(config["_id"]))
            self.assertEqual(
                await autorotate._run_group_cycle(config, object(), [first, second]),
                0,
            )
            paused = await autorotate.get_config(str(config["_id"]))
            self.assertEqual(paused["status"], "paused")
            self.assertEqual(paused["pending_completed_group_ids"], [101])
            self.assertEqual(paused["pending_message_id"], second["_id"])
            self.assertEqual(paused["pending_message_index"], 1)
            self.assertEqual(paused["group_ids"], [101, 202])
            recovery = autorotate._recovery_jobs[str(config["_id"])]
            await recovery
            await autorotate._jobs[str(config["_id"])]

        self.assertEqual(attempted, [101, 202, 202])
        completed = await autorotate.get_config(str(config["_id"]))
        self.assertEqual(completed["status"], "stopped")
        self.assertEqual(completed["next_message_index"], 0)
        self.assertEqual(completed["last_message_id"], second["_id"])
        self.assertNotIn("pending_message_id", completed)

    async def test_configs_persist_independently_and_schedule_once(self):
        first = await self._new_valid_config("first", chat_id=101)
        second = await self._new_valid_config("second", chat_id=202)
        self.assertNotEqual(first["_id"], second["_id"])
        self.assertEqual(len(await self.db.autorotate_configs.find().to_list(None)), 2)

        duplicate = await autorotate.create_config("duplicate")
        await autorotate.edit_config(
            str(duplicate["_id"]),
            {
                "group_ids": first["group_ids"],
                "message_ids": first["message_ids"],
                "interval_seconds": first["interval_seconds"],
            },
            reset_rotation=True,
        )
        started, error = await autorotate.start_config(str(first["_id"]), object())
        self.assertFalse(started)
        self.assertIn("identical rotation", error)
        self.assertTrue(await autorotate.delete_config(str(duplicate["_id"])))

        async def wait_forever(config_id, bot):
            await asyncio.Event().wait()

        with patch.object(autorotate, "_run_config", new=wait_forever):
            started, _ = await autorotate.start_config(str(first["_id"]), object())
            self.assertTrue(started)
            self.assertFalse(autorotate._ensure_job(first, object()))
            self.assertEqual(autorotate.active_job_count(), 1)

            self.assertTrue(await autorotate.pause_config(str(first["_id"])))
            paused = await autorotate.get_config(str(first["_id"]))
            self.assertEqual(paused["status"], "paused")

            resumed, _ = await autorotate.resume_config(str(first["_id"]), object())
            self.assertTrue(resumed)
            self.assertTrue(await autorotate.stop_config(str(first["_id"])))
            stopped = await autorotate.get_config(str(first["_id"]))
            self.assertEqual(stopped["status"], "stopped")
            self.assertTrue(await autorotate.delete_config(str(second["_id"])))

    async def test_group_and_private_interval_validation_uses_same_allowed_range(self):
        config = await self._new_valid_config(interval=5)
        for interval in (5, 10, 20, 30, 60, 120, 300, 47, 86400):
            config["interval_seconds"] = interval
            self.assertIsNone(await autorotate._validate_for_start(config, test_mode=True))
            self.assertIsNone(await autorotate._validate_for_start(config, test_mode=False))
        for interval in (0, 1, 4, 6, 9):
            config["interval_seconds"] = interval
            for test_mode in (True, False):
                error = await autorotate._validate_for_start(config, test_mode=test_mode)
                self.assertIn("Intervals below 10 seconds are unavailable", error)
        config["interval_seconds"] = 86401
        self.assertIn("cannot exceed 86400", await autorotate._validate_for_start(config, test_mode=False))

    async def test_custom_interval_persists_and_scheduler_recovery_uses_it_once(self):
        config = await self._new_valid_config(interval=300)
        await self.db.autorotate_configs.update_one(
            {"_id": config["_id"]},
            {"$set": {
                "status": "paused",
                "next_run_at": autorotate._now() + autorotate.timedelta(seconds=300),
            }},
        )
        self.assertTrue(
            await autorotate.edit_config(str(config["_id"]), {"interval_seconds": 37})
        )
        persisted = await autorotate.get_config(str(config["_id"]))
        self.assertEqual(persisted["interval_seconds"], 37)
        self.assertLessEqual(persisted["next_run_at"], autorotate._now())
        await self.db.autorotate_configs.update_one(
            {"_id": config["_id"]},
            {"$set": {
                "status": "running",
                "test_mode": True,
                "next_run_at": autorotate._now() - autorotate.timedelta(seconds=1),
            }},
        )

        message = {"_id": config["message_ids"][0], "type": "text", "text": "saved"}
        cycle_count = 0
        sleep_delays = []

        async def run_private_cycle(current, bot, messages):
            nonlocal cycle_count
            cycle_count += 1
            await autorotate._finish_cycle(current, messages[0], 0)
            if cycle_count == 2:
                await self.db.autorotate_configs.update_one(
                    {"_id": current["_id"]},
                    {"$set": {"status": "stopped"}},
                )
            return int(current["interval_seconds"])

        async def record_sleep(delay):
            sleep_delays.append(delay)

        with (
            patch.object(autorotate, "_load_selected_messages", new=AsyncMock(return_value=[message])),
            patch.object(autorotate, "_run_private_test", new=run_private_cycle),
            patch.object(autorotate.asyncio, "sleep", new=record_sleep),
        ):
            self.assertEqual(await autorotate.restore_running_configs(object()), 1)
            task = autorotate._jobs[str(config["_id"])]
            await task

        self.assertEqual(cycle_count, 2)
        self.assertEqual(len(sleep_delays), 1)
        self.assertAlmostEqual(sleep_delays[0], 36, delta=0.1)
        recovered = await autorotate.get_config(str(config["_id"]))
        self.assertEqual(recovered["interval_seconds"], 37)
        self.assertEqual(recovered["status"], "stopped")

    async def test_floodwait_is_persisted_and_blocks_resume(self):
        config = await self._new_valid_config()
        await self.db.autorotate_configs.update_one(
            {"_id": config["_id"]},
            {"$set": {"status": "running"}},
        )
        await autorotate._pause_after_wait(config, 120, "FloodWait")
        paused = await autorotate.get_config(str(config["_id"]))
        self.assertEqual(paused["status"], "paused")
        self.assertIsNotNone(paused.get("resume_after"))
        self.assertTrue(paused["auto_resume"])
        started, message = await autorotate.start_config(str(config["_id"]), object())
        self.assertFalse(started)
        self.assertIn("cooldown is active", message)
        cooldown = await self.db.state.find_one({"key": "autorotate_floodwait_until"})
        self.assertEqual(cooldown["value"], paused["resume_after"])

    async def test_floodwait_automatically_resumes_after_exact_wait(self):
        config = await self._new_valid_config()
        await self.db.autorotate_configs.update_one(
            {"_id": config["_id"]},
            {"$set": {"status": "running"}},
        )
        current_time = [autorotate._now()]
        sleep_delays = []
        original_sleep = asyncio.sleep

        async def advance_time(delay):
            sleep_delays.append(delay)
            current_time[0] += autorotate.timedelta(seconds=delay)
            await original_sleep(0)

        async def wait_forever(config_id, bot):
            await asyncio.Event().wait()

        with (
            patch.object(autorotate, "_now", side_effect=lambda: current_time[0]),
            patch.object(autorotate.asyncio, "sleep", new=advance_time),
            patch.object(autorotate, "_run_config", new=wait_forever),
        ):
            await autorotate._pause_after_wait(config, 30, "FloodWait", object())
            paused = await autorotate.get_config(str(config["_id"]))
            recovery = autorotate._recovery_jobs[str(config["_id"])]
            await recovery

        resumed = await autorotate.get_config(str(config["_id"]))
        self.assertEqual(sleep_delays, [30])
        self.assertEqual(current_time[0], paused["resume_after"])
        self.assertEqual(resumed["status"], "running")
        self.assertNotIn("resume_after", resumed)
        self.assertEqual(resumed["next_message_index"], config["next_message_index"])
        await autorotate.shutdown()

    async def test_pending_floodwait_recovery_restores_after_restart(self):
        config = await self._new_valid_config()
        await self.db.autorotate_configs.update_one(
            {"_id": config["_id"]},
            {"$set": {"status": "running"}},
        )
        current_time = [autorotate._now()]
        original_sleep = asyncio.sleep

        async def advance_time(delay):
            current_time[0] += autorotate.timedelta(seconds=delay)
            await original_sleep(0)

        async def wait_forever(config_id, bot):
            await asyncio.Event().wait()

        with (
            patch.object(autorotate, "_now", side_effect=lambda: current_time[0]),
            patch.object(autorotate.asyncio, "sleep", new=advance_time),
            patch.object(autorotate, "_run_config", new=wait_forever),
        ):
            await autorotate._pause_after_wait(config, 45, "FloodWait")
            deadline = (await autorotate.get_config(str(config["_id"])))["resume_after"]
            self.assertEqual(autorotate._recovery_jobs, {})
            self.assertEqual(await autorotate.restore_running_configs(object()), 0)
            recovery = autorotate._recovery_jobs[str(config["_id"])]
            await recovery

        restored = await autorotate.get_config(str(config["_id"]))
        self.assertEqual(restored["status"], "running")
        self.assertEqual(current_time[0], deadline)
        await autorotate.shutdown()

    async def test_multiple_floodwait_recoveries_are_independent(self):
        first = await self._new_valid_config("first", chat_id=101)
        second = await self._new_valid_config("second", chat_id=202)
        for config in (first, second):
            await self.db.autorotate_configs.update_one(
                {"_id": config["_id"]},
                {"$set": {"status": "running"}},
            )

        current_time = [autorotate._now()]
        sleep_delays = []
        original_sleep = asyncio.sleep

        async def advance_time(delay):
            sleep_delays.append(delay)
            current_time[0] += autorotate.timedelta(seconds=delay)
            await original_sleep(0)

        async def wait_forever(config_id, bot):
            await asyncio.Event().wait()

        with (
            patch.object(autorotate, "_now", side_effect=lambda: current_time[0]),
            patch.object(autorotate.asyncio, "sleep", new=advance_time),
            patch.object(autorotate, "_run_config", new=wait_forever),
        ):
            await autorotate._pause_after_wait(first, 10, "FloodWait", object())
            await autorotate._pause_after_wait(second, 25, "FloodWait", object())
            first_deadline = (await autorotate.get_config(str(first["_id"])))["resume_after"]
            second_deadline = (await autorotate.get_config(str(second["_id"])))["resume_after"]
            first_recovery = autorotate._recovery_jobs[str(first["_id"])]
            second_recovery = autorotate._recovery_jobs[str(second["_id"])]
            self.assertIsNot(first_recovery, second_recovery)
            await asyncio.gather(first_recovery, second_recovery)

        self.assertEqual((first_deadline - current_time[0]).total_seconds(), -15)
        self.assertEqual((second_deadline - first_deadline).total_seconds(), 15)
        self.assertEqual(
            (await autorotate.get_config(str(first["_id"])))["status"],
            "running",
        )
        self.assertEqual(
            (await autorotate.get_config(str(second["_id"])))["status"],
            "running",
        )
        await autorotate.shutdown()

    async def test_manual_pause_and_stop_cancel_pending_auto_recovery(self):
        paused = await self._new_valid_config("manual-pause", chat_id=101)
        stopped = await self._new_valid_config("manual-stop", chat_id=202)
        current_time = [autorotate._now()]
        original_sleep = asyncio.sleep

        async def advance_time(delay):
            current_time[0] += autorotate.timedelta(seconds=delay)
            await original_sleep(0)

        async def wait_forever(config_id, bot):
            await asyncio.Event().wait()

        with (
            patch.object(autorotate, "_now", side_effect=lambda: current_time[0]),
            patch.object(autorotate.asyncio, "sleep", new=advance_time),
            patch.object(autorotate, "_run_config", new=wait_forever),
        ):
            for config in (paused, stopped):
                await self.db.autorotate_configs.update_one(
                    {"_id": config["_id"]},
                    {"$set": {"status": "running"}},
                )
                await autorotate._pause_after_wait(config, 20, "FloodWait", object())

            self.assertTrue(await autorotate.pause_config(str(paused["_id"])))
            self.assertTrue(await autorotate.stop_config(str(stopped["_id"])))
            current_time[0] += autorotate.timedelta(seconds=30)
            self.assertEqual(await autorotate.restore_running_configs(object()), 0)

        paused_after = await autorotate.get_config(str(paused["_id"]))
        stopped_after = await autorotate.get_config(str(stopped["_id"]))
        self.assertEqual(paused_after["status"], "paused")
        self.assertFalse(paused_after.get("auto_resume", False))
        self.assertEqual(stopped_after["status"], "stopped")
        self.assertFalse(stopped_after.get("auto_resume", False))
        await autorotate.shutdown()

    async def test_repeated_floodwait_schedules_a_new_recovery(self):
        config = await self._new_valid_config()
        await self.db.autorotate_configs.update_one(
            {"_id": config["_id"]},
            {"$set": {"status": "running"}},
        )
        current_time = [autorotate._now()]
        initial_time = current_time[0]
        original_sleep = asyncio.sleep

        async def advance_time(delay):
            current_time[0] += autorotate.timedelta(seconds=delay)
            await original_sleep(0)

        async def wait_forever(config_id, bot):
            await asyncio.Event().wait()

        with (
            patch.object(autorotate, "_now", side_effect=lambda: current_time[0]),
            patch.object(autorotate.asyncio, "sleep", new=advance_time),
            patch.object(autorotate, "_run_config", new=wait_forever),
        ):
            await autorotate._pause_after_wait(config, 10, "FloodWait", object())
            first_recovery = autorotate._recovery_jobs[str(config["_id"])]
            await first_recovery
            await autorotate._cancel_job(config["_id"])

            resumed = await autorotate.get_config(str(config["_id"]))
            await autorotate._pause_after_wait(resumed, 20, "FloodWait", object())
            second_recovery = autorotate._recovery_jobs[str(config["_id"])]
            self.assertIsNot(first_recovery, second_recovery)
            await second_recovery

        self.assertEqual(current_time[0], initial_time + autorotate.timedelta(seconds=30))
        self.assertEqual(
            (await autorotate.get_config(str(config["_id"])))["status"],
            "running",
        )
        await autorotate.shutdown()

    async def test_floodwait_during_auto_resume_schedules_another_recovery(self):
        first = {"_id": ObjectId(), "type": "text", "text": "first"}
        second = {"_id": ObjectId(), "type": "text", "text": "second"}
        await self.db.messages.insert_one(first)
        await self.db.messages.insert_one(second)
        await self.db.groups.insert_one({"chat_id": 101, "title": "One"})
        config = await autorotate.create_config("repeated-auto-recovery")
        await autorotate.edit_config(
            str(config["_id"]),
            {"group_ids": [101], "message_ids": [first["_id"], second["_id"]]},
            reset_rotation=True,
        )
        await self.db.autorotate_configs.update_one(
            {"_id": config["_id"]},
            {"$set": {
                "status": "running",
                "next_message_index": 1,
                "last_message_id": first["_id"],
            }},
        )
        current_time = [autorotate._now()]
        original_sleep = asyncio.sleep
        send_attempts = 0

        async def advance_time(delay):
            current_time[0] += autorotate.timedelta(seconds=delay)
            await original_sleep(0)

        async def send_with_second_floodwait(bot, chat_id, message):
            nonlocal send_attempts
            send_attempts += 1
            if send_attempts == 1:
                raise autorotate.FloodWait(13, "FLOOD_WAIT_X")

        async def run_resumed_cycle(config_id, bot):
            current = await autorotate.get_config(str(config_id))
            messages = await autorotate._load_selected_messages(current)
            await autorotate._run_group_cycle(current, bot, messages)
            if send_attempts > 1:
                await self.db.autorotate_configs.update_one(
                    {"_id": config_id},
                    {"$set": {"status": "stopped"}},
                )

        with (
            patch.object(autorotate, "_now", side_effect=lambda: current_time[0]),
            patch.object(autorotate.asyncio, "sleep", new=advance_time),
            patch.object(autorotate, "_send_group", new=send_with_second_floodwait),
            patch.object(autorotate, "_run_config", new=run_resumed_cycle),
        ):
            config = await autorotate.get_config(str(config["_id"]))
            await autorotate._pause_after_wait(config, 7, "FloodWait", object())
            key = str(config["_id"])
            while key in autorotate._recovery_jobs:
                await autorotate._recovery_jobs[key]

        persisted = await autorotate.get_config(str(config["_id"]))
        self.assertEqual(send_attempts, 2)
        self.assertEqual(persisted["status"], "stopped")
        self.assertEqual(persisted["next_message_index"], 0)
        self.assertEqual(persisted["last_message_id"], second["_id"])
        self.assertEqual(current_time[0] - config["updated_at"], autorotate.timedelta(seconds=20))
        await autorotate.shutdown()

    async def test_only_one_recovery_job_is_scheduled_per_configuration(self):
        config = await self._new_valid_config()
        await self.db.autorotate_configs.update_one(
            {"_id": config["_id"]},
            {"$set": {"status": "running"}},
        )
        await autorotate._pause_after_wait(config, 120, "FloodWait")
        pending = await autorotate.get_config(str(config["_id"]))

        self.assertTrue(autorotate._ensure_recovery_job(pending, object()))
        task = autorotate._recovery_jobs[str(config["_id"])]
        self.assertFalse(autorotate._ensure_recovery_job(pending, object()))
        self.assertIs(autorotate._recovery_jobs[str(config["_id"])], task)
        self.assertEqual(len(autorotate._recovery_jobs), 1)

    async def test_running_configuration_restores_after_scheduler_restart(self):
        config = await self._new_valid_config()
        await self.db.autorotate_configs.update_one(
            {"_id": config["_id"]},
            {"$set": {"status": "running"}},
        )

        async def wait_forever(config_id, bot):
            await asyncio.Event().wait()

        with patch.object(autorotate, "_run_config", new=wait_forever):
            self.assertEqual(await autorotate.restore_running_configs(object()), 1)
            self.assertEqual(await autorotate.restore_running_configs(object()), 0)
            await autorotate.shutdown()
            self.assertEqual(await autorotate.restore_running_configs(object()), 1)


if __name__ == "__main__":
    unittest.main()
