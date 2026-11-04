import asyncio
import base64
import copy
import io
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

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
                "interval_seconds": 60,
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
        await self.db.messages.insert_one(first)
        await self.db.groups.insert_one({"chat_id": 101, "title": "One"})
        await self.db.groups.insert_one({"chat_id": 202, "title": "Two"})
        config = await autorotate.create_config("flood-resume")
        await autorotate.edit_config(
            str(config["_id"]),
            {"group_ids": [101, 202], "message_ids": [first["_id"]]},
            reset_rotation=True,
        )
        attempted = []

        class FakeFloodWait(Exception):
            def __init__(self, value):
                self.value = value

        async def send_with_wait(bot, chat_id, message):
            attempted.append(chat_id)
            if chat_id == 202 and attempted.count(202) == 1:
                raise FakeFloodWait(30)

        with patch.object(autorotate, "FloodWait", FakeFloodWait), patch.object(
            autorotate, "_send_group", new=send_with_wait
        ):
            config = await autorotate.get_config(str(config["_id"]))
            self.assertEqual(await autorotate._run_group_cycle(config, object(), [first]), 0)
            paused = await autorotate.get_config(str(config["_id"]))
            self.assertEqual(paused["status"], "paused")
            self.assertEqual(paused["pending_completed_group_ids"], [101])

            await self.db.autorotate_configs.update_one(
                {"_id": config["_id"]},
                {"$set": {"status": "running", "resume_after": None}},
            )
            resumed = await autorotate.get_config(str(config["_id"]))
            await autorotate._run_group_cycle(resumed, object(), [first])

        self.assertEqual(attempted, [101, 202, 202])

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

    async def test_group_rotation_rejects_fast_intervals_but_private_test_allows_them(self):
        config = await self._new_valid_config(interval=30)
        error = await autorotate._validate_for_start(config, test_mode=False)
        self.assertIn("at least 1 minute", error)
        self.assertIsNone(await autorotate._validate_for_start(config, test_mode=True))

    async def test_floodwait_is_persisted_and_blocks_resume(self):
        config = await self._new_valid_config()
        await autorotate._pause_after_wait(config, 120, "FloodWait")
        paused = await autorotate.get_config(str(config["_id"]))
        self.assertEqual(paused["status"], "paused")
        self.assertIsNotNone(paused.get("resume_after"))
        started, message = await autorotate.start_config(str(config["_id"]), object())
        self.assertFalse(started)
        self.assertIn("cooldown is active", message)
        cooldown = await self.db.state.find_one({"key": "autorotate_floodwait_until"})
        self.assertEqual(cooldown["value"], paused["resume_after"])

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
