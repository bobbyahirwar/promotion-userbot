import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.update({
    "BOT_TOKEN": "123:validation-token",
    "API_ID": "1",
    "API_HASH": "validation-hash",
    "SESSION_STRING": "",
    "OWNER_ID": "1",
    "MONGO_URI": "mongodb://127.0.0.1:27017/promotionbot",
})

try:
    asyncio.get_running_loop()
    _IMPORT_LOOP = None
except RuntimeError:
    _IMPORT_LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(_IMPORT_LOOP)

from core.account_context import account_context, current_account_id
from database import AccountDatabase
from pyrogram.enums import ChatMemberStatus

from core import promotion
from handlers import sync_groups

if _IMPORT_LOOP:
    _IMPORT_LOOP.close()
    asyncio.set_event_loop(None)


class _Cursor:
    def __init__(self, documents):
        self.documents = list(documents)

    def sort(self, *_args):
        return self

    async def to_list(self, length):
        return self.documents if length is None else self.documents[:length]


class _Collection:
    def __init__(self):
        self.documents = []

    @staticmethod
    def _matches(document, query):
        for key, value in query.items():
            if key == "$and":
                if not all(_Collection._matches(document, item) for item in value):
                    return False
            elif isinstance(value, dict) and "$in" in value:
                if document.get(key) not in value["$in"]:
                    return False
            elif document.get(key) != value:
                return False
        return True

    async def find_one(self, query=None, **_kwargs):
        return next(
            (doc.copy() for doc in self.documents if self._matches(doc, query or {})),
            None,
        )

    def find(self, query=None, *_args, **_kwargs):
        return _Cursor([
            doc.copy() for doc in self.documents
            if self._matches(doc, query or {})
        ])

    async def update_one(self, query, update, upsert=False, **_kwargs):
        for doc in self.documents:
            if self._matches(doc, query):
                doc.update(update.get("$set", {}))
                for key in update.get("$unset", {}):
                    doc.pop(key, None)
                return SimpleNamespace(matched_count=1, upserted_id=None)
        if not upsert:
            return SimpleNamespace(matched_count=0, upserted_id=None)
        doc = {key: value for key, value in query.items() if not key.startswith("$")}
        doc.update(update.get("$setOnInsert", {}))
        doc.update(update.get("$set", {}))
        doc["_id"] = len(self.documents) + 1
        self.documents.append(doc)
        return SimpleNamespace(matched_count=0, upserted_id=doc["_id"])

    async def delete_one(self, query, **_kwargs):
        for index, doc in enumerate(self.documents):
            if self._matches(doc, query):
                self.documents.pop(index)
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    async def delete_many(self, query, **_kwargs):
        before = len(self.documents)
        self.documents[:] = [
            doc for doc in self.documents if not self._matches(doc, query)
        ]
        return SimpleNamespace(deleted_count=before - len(self.documents))

    async def update_many(self, query, update, **_kwargs):
        count = 0
        for doc in self.documents:
            if self._matches(doc, query):
                doc.update(update.get("$set", {}))
                count += 1
        return SimpleNamespace(matched_count=count)


class _Database:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, _Collection())


class _Dialogs:
    def __init__(self, chats):
        self.chats = chats

    async def get_dialogs(self):
        for chat in self.chats:
            yield SimpleNamespace(chat=chat)


class GroupSyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.raw = _Database()

        def scoped_db():
            return AccountDatabase(self.raw, current_account_id())

        self.sync_db_patch = patch("handlers.sync_groups.get_db", side_effect=scoped_db)
        self.inactive_db_patch = patch(
            "handlers.inactive_groups.get_db", side_effect=scoped_db
        )
        self.sync_db_patch.start()
        self.inactive_db_patch.start()

    async def asyncTearDown(self):
        self.sync_db_patch.stop()
        self.inactive_db_patch.stop()

    async def test_sync_and_departure_are_isolated_for_same_chat_id(self):
        chat = SimpleNamespace(
            id=-10055,
            type=sync_groups.ChatType.SUPERGROUP,
            title="Shared group",
            username=None,
        )
        with patch("handlers.sync_groups.userbot", _Dialogs([chat])):
            with account_context(101):
                await sync_groups.sync_joined_groups()
            with account_context(202):
                await sync_groups.sync_joined_groups()

        groups = self.raw["account_groups"].documents
        self.assertEqual(
            {(doc["account_id"], doc["chat_id"]) for doc in groups},
            {(101, -10055), (202, -10055)},
        )
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(doc["promotion_authorized"] is True for doc in groups))
        self.assertTrue(all(doc["membership_status"] == "member" for doc in groups))

        with account_context(101):
            await AccountDatabase(self.raw, 101).groups.update_one(
                {"chat_id": -10055},
                {"$set": {"promotion_authorized": False}},
            )
        with patch("handlers.sync_groups.userbot", _Dialogs([chat])):
            with account_context(101):
                await sync_groups.sync_joined_groups()
        groups = self.raw["account_groups"].documents
        self.assertEqual(len(groups), 2)
        self.assertTrue(
            (await AccountDatabase(self.raw, 101).groups.find_one(
                {"chat_id": -10055}
            ))["promotion_authorized"]
        )

        with patch("handlers.sync_groups.userbot", _Dialogs([])):
            with account_context(101):
                await sync_groups.sync_joined_groups()

        one = await AccountDatabase(self.raw, 101).groups.find_one({"chat_id": -10055})
        two = await AccountDatabase(self.raw, 202).groups.find_one({"chat_id": -10055})
        self.assertFalse(one["active"])
        self.assertEqual(one["membership_status"], "left")
        self.assertTrue(two["active"])
        self.assertIsNotNone(
            await AccountDatabase(self.raw, 101).inactive_groups.find_one(
                {"chat_id": -10055}
            )
        )
        self.assertIsNone(
            await AccountDatabase(self.raw, 202).inactive_groups.find_one(
                {"chat_id": -10055}
            )
        )

        with patch("handlers.sync_groups.userbot", _Dialogs([chat])):
            with account_context(101):
                _, _, _, _, reactivated = await sync_groups.sync_joined_groups()
        self.assertEqual(reactivated, 1)
        self.assertTrue(
            (await AccountDatabase(self.raw, 101).groups.find_one(
                {"chat_id": -10055}
            ))["active"]
        )

    async def test_membership_update_and_send_eligibility_are_account_scoped(self):
        for account_id in (101, 202):
            with account_context(account_id):
                await sync_groups._store_joined_group(
                    SimpleNamespace(id=-10077, title="Group", username=None)
                )

        class _Client:
            def add_handler(self, handler):
                self.handler = handler

        client = _Client()
        sync_groups.register_membership_updates(client, 101)
        event = SimpleNamespace(
            new_chat_member=SimpleNamespace(
                user=SimpleNamespace(id=101),
                status=ChatMemberStatus.BANNED,
            ),
            chat=SimpleNamespace(
                id=-10077,
                type=sync_groups.ChatType.SUPERGROUP,
            ),
        )
        await client.handler.callback(client, event)

        one = await AccountDatabase(self.raw, 101).groups.find_one({"chat_id": -10077})
        two = await AccountDatabase(self.raw, 202).groups.find_one({"chat_id": -10077})
        self.assertFalse(one["active"])
        self.assertEqual(one["membership_status"], "banned")
        self.assertTrue(two["active"])

        class _Userbot:
            async def get_chat(self, _chat_id):
                return SimpleNamespace(
                    type=sync_groups.ChatType.SUPERGROUP,
                    permissions=None,
                )

            async def get_chat_member(self, _chat_id, _account_id):
                return SimpleNamespace(
                    status=ChatMemberStatus.MEMBER,
                    can_send_messages=None,
                    can_post_messages=None,
                )

        with account_context(101):
            await sync_groups._store_joined_group(
                SimpleNamespace(id=-10077, title="Group", username=None)
            )
        with (
            patch("core.promotion.userbot", _Userbot()),
            account_context(101),
        ):
            result = await promotion._check_group_eligibility(
                AccountDatabase(self.raw, 101),
                {"chat_id": -10077},
                101,
            )
        self.assertEqual(result[0], "eligible")

        class _ReadOnlyUserbot(_Userbot):
            async def get_chat_member(self, _chat_id, _account_id):
                return SimpleNamespace(
                    status=ChatMemberStatus.MEMBER,
                    can_send_messages=False,
                    can_post_messages=None,
                )

        with (
            patch("core.promotion.userbot", _ReadOnlyUserbot()),
            account_context(101),
        ):
            result = await promotion._check_group_eligibility(
                AccountDatabase(self.raw, 101),
                {"chat_id": -10077},
                101,
            )
        self.assertEqual(result[0], "skipped")
        self.assertTrue(
            (await AccountDatabase(self.raw, 101).groups.find_one(
                {"chat_id": -10077}
            ))["active"]
        )

        with account_context(101):
            await AccountDatabase(self.raw, 101).state.update_one(
                {"key": "promotion_slowmode_until:-10077"},
                {"$set": {"value": datetime.now(timezone.utc) + timedelta(seconds=30)}},
                upsert=True,
            )
        with (
            patch("core.promotion.userbot", _Userbot()),
            account_context(101),
        ):
            result = await promotion._check_group_eligibility(
                AccountDatabase(self.raw, 101),
                {"chat_id": -10077},
                101,
            )
        self.assertEqual(result, ("skipped", "Slowmode cooldown is active"))

        with account_context(101):
            await AccountDatabase(self.raw, 101).state.update_one(
                {"key": "promotion_slowmode_until:-10077"},
                {"$set": {"value": datetime.now(timezone.utc) - timedelta(seconds=1)}},
            )

        class _FakeFloodWait(Exception):
            value = 45

        class _FloodingUserbot(_Userbot):
            async def get_chat(self, _chat_id):
                raise _FakeFloodWait()

        with (
            patch("core.promotion.userbot", _FloodingUserbot()),
            patch("core.promotion.FloodWait", _FakeFloodWait),
            account_context(101),
        ):
            with self.assertRaises(_FakeFloodWait):
                await promotion._check_group_eligibility(
                    AccountDatabase(self.raw, 101),
                    {"chat_id": -10077},
                    101,
                )
        self.assertTrue(
            (await AccountDatabase(self.raw, 101).groups.find_one(
                {"chat_id": -10077}
            ))["active"]
        )

        with account_context(202):
            await AccountDatabase(self.raw, 202).groups.update_one(
                {"chat_id": -10077},
                {"$set": {"promotion_authorized": False}},
            )
        with (
            patch("core.promotion.userbot", _Userbot()),
            account_context(202),
        ):
            result = await promotion._check_group_eligibility(
                AccountDatabase(self.raw, 202),
                {"chat_id": -10077},
                202,
            )
        self.assertEqual(result[0], "eligible")

    async def test_promotion_start_stop_does_not_create_duplicate_account_tasks(self):
        state = SimpleNamespace(
            update_one=AsyncMock(),
            delete_many=AsyncMock(),
        )
        fake_db = SimpleNamespace(state=state)
        waiting = asyncio.Event()

        async def wait_for_stop(*_args):
            await waiting.wait()

        with (
            patch("core.promotion.get_db", return_value=fake_db),
            patch("handlers.sync_groups.sync_joined_groups", new=AsyncMock()),
            patch.object(promotion, "_loop", new=wait_for_stop),
            patch.object(promotion, "_membership_sync_loop", new=wait_for_stop),
        ):
            self.assertTrue(await promotion.start_promotion(object(), 303))
            first_task = promotion._tasks[303]
            self.assertFalse(await promotion.start_promotion(object(), 303))
            self.assertIs(promotion._tasks[303], first_task)
            self.assertTrue(promotion.is_running(303))
            await promotion.stop_promotion(303)
            self.assertFalse(promotion.is_running(303))
            self.assertNotIn(303, promotion._sync_tasks)


if __name__ == "__main__":
    unittest.main()
