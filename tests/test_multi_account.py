import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pymongo.errors import DuplicateKeyError

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

from core.accounts import AccountError, AccountManager
from database import AccountDatabase
from pyrogram.errors import SessionRevoked

if _IMPORT_LOOP:
    _IMPORT_LOOP.close()
    asyncio.set_event_loop(None)


class _Cursor:
    def __init__(self, documents):
        self.documents = list(documents)

    def sort(self, *args):
        return self

    async def to_list(self, length):
        return self.documents if length is None else self.documents[:length]


class _AccountCollection:
    def __init__(self, documents=None):
        self.documents = list(documents or [])
        self.insert_error = None
        self.update_one_error = None
        self.update_many_error = None

    async def find_one(self, query):
        def matches(document, condition):
            if "$or" in condition:
                return any(matches(document, item) for item in condition["$or"])
            return all(document.get(key) == value for key, value in condition.items())

        return next(
            (doc.copy() for doc in self.documents if matches(doc, query)),
            None,
        )

    def find(self, query=None, projection=None):
        query = query or {}
        documents = []

        def matches(document, condition):
            if "$or" in condition:
                return any(matches(document, item) for item in condition["$or"])
            return all(document.get(key) == value for key, value in condition.items())

        for doc in self.documents:
            if not matches(doc, query):
                continue
            result = doc.copy()
            if projection:
                for key, exclude in projection.items():
                    if not exclude:
                        result.pop(key, None)
            documents.append(result)
        return _Cursor(documents)

    async def insert_one(self, document):
        if self.insert_error:
            raise self.insert_error
        stored = document.copy()
        stored["_id"] = len(self.documents) + 1
        self.documents.append(stored)
        return SimpleNamespace(inserted_id=stored["_id"])

    async def update_one(self, query, update, upsert=False):
        if self.update_one_error:
            raise self.update_one_error

        def matches(document, condition):
            if "$or" in condition:
                return any(matches(document, item) for item in condition["$or"])
            return all(document.get(key) == value for key, value in condition.items())

        for document in self.documents:
            if not matches(document, query):
                continue
            document.update(update.get("$set", {}))
            if "$setOnInsert" in update and not update.get("$set"):
                document.update(update["$setOnInsert"])
            for key in update.get("$unset", {}):
                document.pop(key, None)
            return SimpleNamespace(matched_count=1, modified_count=1)
        if upsert:
            stored = {
                key: value for key, value in query.items()
                if not key.startswith("$")
            }
            stored.update(update.get("$setOnInsert", {}))
            stored["_id"] = len(self.documents) + 1
            self.documents.append(stored)
            return SimpleNamespace(
                matched_count=0,
                modified_count=0,
                upserted_id=stored["_id"],
            )
        return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=None)

    async def update_many(self, query, update):
        if self.update_many_error:
            raise self.update_many_error
        count = 0
        for document in self.documents:
            if all(document.get(key) == value for key, value in query.items()):
                document.update(update.get("$set", {}))
                count += 1
        return SimpleNamespace(matched_count=count, modified_count=count)


class _FakeClient:
    def __init__(self, user_id, start_error=None):
        self.user_id = user_id
        self.start_error = start_error
        self.is_connected = False
        self.stopped = False

    async def start(self):
        if self.start_error:
            raise self.start_error
        self.is_connected = True
        return self

    async def get_me(self):
        return SimpleNamespace(
            id=self.user_id,
            first_name=f"Account {self.user_id}",
            last_name=None,
            username=f"user{self.user_id}",
        )

    async def stop(self):
        self.is_connected = False
        self.stopped = True


class _SpyCollection:
    def __init__(self):
        self.calls = []

    async def insert_one(self, document, *args, **kwargs):
        self.calls.append(("insert_one", document, args, kwargs))

    async def update_one(self, query, update, *args, **kwargs):
        self.calls.append(("update_one", query, update, args, kwargs))

    async def delete_many(self, query, *args, **kwargs):
        self.calls.append(("delete_many", query, args, kwargs))

    def find(self, query, *args, **kwargs):
        self.calls.append(("find", query, args, kwargs))
        return object()


class _FakeDatabase:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, _SpyCollection())


class MultiAccountTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = AccountManager()
        self.raw_db = SimpleNamespace(accounts=_AccountCollection())
        self.manager_patch = patch("core.accounts.get_raw_db", return_value=self.raw_db)
        self.manager_patch.start()
        self.clients = {}

        def make_client(session_string, name):
            user_id = int(session_string.split("-")[-1])
            client = _FakeClient(user_id)
            self.clients[user_id] = client
            return client

        self.client_patch = patch("core.accounts.create_user_client", side_effect=make_client)
        self.client_patch.start()

    async def asyncTearDown(self):
        await self.manager.shutdown()
        self.manager_patch.stop()
        self.client_patch.stop()

    async def test_accounts_store_sessions_select_independently_and_remove_one(self):
        first = await self.manager.add_session("pyrogram-session-101")
        second = await self.manager.add_session("pyrogram-session-202")

        self.assertEqual(self.manager.selected_account_id, 202)
        self.assertTrue(self.clients[101].is_connected)
        self.assertTrue(self.clients[202].is_connected)
        saved = self.raw_db.accounts.documents
        self.assertEqual({doc["account_id"] for doc in saved}, {101, 202})
        self.assertEqual(saved[0]["session_string"], "pyrogram-session-101")
        self.assertNotIn("encrypted_session", saved[0])
        listed = await self.manager.list_accounts()
        self.assertTrue(all("session_string" not in account for account in listed))

        self.assertTrue(await self.manager.select_account(first["account_id"]))
        self.assertEqual(self.manager.selected_account_id, 101)

        with (
            patch("core.promotion.stop_promotion", new=AsyncMock()),
            patch("core.autorotate.shutdown_account", new=AsyncMock()),
        ):
            self.assertTrue(await self.manager.remove_account(first["account_id"]))

        self.assertFalse(self.clients[101].is_connected)
        self.assertTrue(self.clients[202].is_connected)
        self.assertEqual(self.manager.selected_account_id, 202)
        self.assertFalse(saved[0]["active"])
        self.assertTrue(saved[1]["active"])
        self.assertNotIn("session_string", saved[0])
        self.assertNotIn("encrypted_session", saved[0])

    async def test_clients_use_shared_api_credentials_with_independent_sessions(self):
        from core import userbot
        from config import API_HASH, API_ID

        with patch.object(userbot, "_SafeClient") as client_factory:
            userbot.create_user_client("unique-session-101", "account-101")
            userbot.create_user_client("unique-session-202", "account-202")

        self.assertEqual(client_factory.call_count, 2)
        first, second = (call.kwargs for call in client_factory.call_args_list)
        self.assertEqual(first["api_id"], API_ID)
        self.assertEqual(second["api_id"], API_ID)
        self.assertEqual(first["api_hash"], API_HASH)
        self.assertEqual(second["api_hash"], API_HASH)
        self.assertEqual(first["session_string"], "unique-session-101")
        self.assertEqual(second["session_string"], "unique-session-202")

    async def test_duplicate_identity_reuses_connected_client_without_another_record(self):
        first = await self.manager.add_session("pyrogram-session-101")
        original_client = self.manager.clients[101]

        duplicate = await self.manager.add_session("different-session-101")

        self.assertTrue(duplicate["already_registered"])
        self.assertEqual(duplicate["account_id"], first["account_id"])
        self.assertEqual(len(self.raw_db.accounts.documents), 1)
        self.assertIs(self.manager.clients[101], original_client)
        self.assertTrue(original_client.is_connected)
        self.assertTrue(self.clients[101].stopped)

    async def test_legacy_record_is_found_by_telegram_id_and_updated_in_place(self):
        legacy_record = {
            "_id": 101,
            "account_id": 101,
            "session_string": "legacy-session",
            "active": True,
            "unrelated_setting": "keep",
        }
        self.raw_db.accounts.documents.append(legacy_record.copy())

        result = await self.manager.add_session("pyrogram-session-101")

        self.assertTrue(result["already_registered"])
        self.assertEqual(len(self.raw_db.accounts.documents), 1)
        saved = self.raw_db.accounts.documents[0]
        self.assertEqual(saved["_id"], 101)
        self.assertEqual(saved["account_id"], 101)
        self.assertEqual(saved["telegram_user_id"], 101)
        self.assertEqual(saved["unrelated_setting"], "keep")
        self.assertEqual(saved["session_string"], "pyrogram-session-101")

    async def test_mongodb_save_failure_is_stage_specific_and_preserves_other_clients(self):
        await self.manager.add_session("pyrogram-session-202")
        self.raw_db.accounts.update_one_error = RuntimeError("database unavailable")

        with self.assertLogs("core.accounts", level="ERROR") as captured:
            with self.assertRaises(AccountError) as raised:
                await self.manager.add_session("pyrogram-session-303")

        self.assertIn("MongoDB account save", str(raised.exception))
        self.assertIn("MongoDB account save", captured.output[0])
        self.assertIn("RuntimeError", captured.output[0])
        self.assertNotIn("database unavailable", captured.output[0])
        self.assertNotIn("pyrogram-session-303", captured.output[0])
        self.assertNotIn("validation-hash", captured.output[0])
        self.assertTrue(self.manager.clients[202].is_connected)
        self.assertNotIn(303, self.manager.clients)
        self.assertTrue(self.clients[303].stopped)

    async def test_duplicate_key_diagnostic_reports_index_without_session_data(self):
        self.raw_db.accounts.update_one_error = DuplicateKeyError(
            "duplicate key value must not be logged",
            code=11000,
            details={
                "indexName": "telegram_user_id_1",
                "keyPattern": {"telegram_user_id": 1},
                "keyValue": {"telegram_user_id": 303},
            },
        )

        with self.assertLogs("core.accounts", level="ERROR") as captured:
            with self.assertRaises(AccountError) as raised:
                await self.manager.add_session("pyrogram-session-303")

        diagnostic = "\n".join(captured.output)
        self.assertIn("index=telegram_user_id_1", diagnostic)
        self.assertIn("keyPattern=[('telegram_user_id', 1)]", diagnostic)
        self.assertIn("keyValue={'telegram_user_id': 303}", diagnostic)
        self.assertNotIn("pyrogram-session-303", diagnostic)
        self.assertNotIn("duplicate key value must not be logged", diagnostic)
        self.assertIn("DuplicateKeyError", str(raised.exception))

    async def test_selection_failure_keeps_saved_account_connected_and_reports_warning(self):
        self.raw_db.accounts.update_many_error = RuntimeError("database unavailable")

        with self.assertLogs("core.accounts", level="ERROR"):
            account = await self.manager.add_session("pyrogram-session-202")

        self.assertTrue(account["selection_warning"])
        self.assertEqual(len(self.raw_db.accounts.documents), 1)
        self.assertTrue(self.manager.clients[202].is_connected)

    async def test_startup_reloads_both_registered_accounts_after_restart(self):
        await self.manager.add_session("pyrogram-session-101")
        await self.manager.add_session("pyrogram-session-202")
        self.assertEqual(len(self.raw_db.accounts.documents), 2)
        await self.manager.shutdown()
        restarted_manager = AccountManager()
        with patch("core.accounts.get_raw_db", return_value=self.raw_db):
            with patch(
                "core.accounts.create_user_client",
                side_effect=lambda session, name: _FakeClient(
                    int(session.split("-")[-1])
                ),
            ):
                await restarted_manager.startup()
                self.assertEqual(set(restarted_manager.clients), {101, 202})
                self.assertEqual(restarted_manager.selected_account_id, 202)
                self.assertTrue(
                    all(client.is_connected for client in restarted_manager.clients.values())
                )
                self.assertEqual(len(self.raw_db.accounts.documents), 2)
                await restarted_manager.shutdown()

    async def test_startup_retains_legacy_encrypted_only_records_without_connecting(self):
        legacy_record = {
            "_id": 101,
            "owner_id": 1,
            "account_id": 101,
            "encrypted_session": "legacy-encrypted-value",
            "active": True,
            "selected": True,
        }
        self.raw_db.accounts.documents.append(legacy_record.copy())

        with self.assertLogs("core.accounts", level="ERROR") as captured:
            await self.manager.startup()

        self.assertIn("MissingSessionString", captured.output[0])
        self.assertEqual(self.raw_db.accounts.documents, [legacy_record])
        self.assertNotIn(101, self.manager.clients)

    async def test_restart_and_reconnect_use_stored_plain_session_without_a_key(self):
        await self.manager.add_session("pyrogram-session-101")
        self.assertEqual(
            self.raw_db.accounts.documents[0]["session_string"],
            "pyrogram-session-101",
        )
        await self.manager.shutdown()

        restarted_manager = AccountManager()
        with patch.dict(os.environ):
            os.environ.pop("SESSION_ENCRYPTION_KEY", None)
            await restarted_manager.startup()
            self.assertTrue(restarted_manager.clients[101].is_connected)
            self.clients[101].is_connected = False
            self.assertTrue(await restarted_manager.reconnect_account(101))
            self.assertTrue(restarted_manager.clients[101].is_connected)
            await restarted_manager.shutdown()

    async def test_invalid_session_is_reported_without_echoing_credentials(self):
        self.client_patch.stop()
        self.client_patch = patch(
            "core.accounts.create_user_client",
            return_value=_FakeClient(0, SessionRevoked("sensitive-session-value")),
        )
        self.client_patch.start()
        with self.assertLogs("core.accounts", level="ERROR") as captured:
            with self.assertRaises(AccountError) as raised:
                await self.manager.add_session("sensitive-session-value")
        self.assertNotIn("sensitive-session-value", str(raised.exception))
        self.assertNotIn("sensitive-session-value", "\n".join(captured.output))
        self.assertIn("invalid, expired, unauthorized", str(raised.exception))

    async def test_reconnecting_one_account_does_not_replace_another_client(self):
        await self.manager.add_session("pyrogram-session-101")
        await self.manager.add_session("pyrogram-session-202")
        other_client = self.manager.clients[202]
        self.clients[101].is_connected = False

        self.assertTrue(await self.manager.reconnect_account(101))
        self.assertTrue(self.manager.clients[101].is_connected)
        self.assertIs(self.manager.clients[202], other_client)
        self.assertTrue(other_client.is_connected)

    async def test_non_owner_cannot_submit_or_manage_an_account(self):
        from handlers.accounts import _callback, _receive_session

        query = SimpleNamespace(
            answer=AsyncMock(),
            data="acct:manage",
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=999),
        )
        await _callback(update, SimpleNamespace(user_data={}))
        query.answer.assert_awaited_once()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])

        context = SimpleNamespace(user_data={"awaiting_account_session": True})
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=999),
            effective_message=SimpleNamespace(text="secret-session"),
        )
        with patch.object(self.manager, "add_session", new=AsyncMock()) as add:
            await _receive_session(update, context)
        add.assert_not_awaited()

    async def test_scoped_collections_filter_same_chat_id_and_tag_every_write(self):
        raw = _FakeDatabase()
        account_one = AccountDatabase(raw, 101)
        account_two = AccountDatabase(raw, 202)

        await account_one.groups.insert_one({"chat_id": -1001, "account_id": 999})
        await account_two.groups.update_one(
            {"chat_id": -1001}, {"$set": {"title": "second account"}}, upsert=True
        )
        account_one.groups.find({"chat_id": -1001})
        await account_two.groups.delete_many({"chat_id": -1001})

        collection = raw["account_groups"]
        inserted = collection.calls[0][1]
        self.assertEqual(inserted["account_id"], 101)
        update_query, update = collection.calls[1][1:3]
        self.assertEqual(update_query["account_id"], 202)
        self.assertEqual(update["$setOnInsert"]["account_id"], 202)
        find_query = collection.calls[2][1]
        self.assertEqual(find_query["account_id"], 101)
        delete_query = collection.calls[3][1]
        self.assertEqual(delete_query["account_id"], 202)
        with self.assertRaises(ValueError):
            await account_one.groups.update_one(
                {"chat_id": -1001},
                {"$set": {"account_id": 202}},
            )


if __name__ == "__main__":
    unittest.main()
