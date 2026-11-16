import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

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

    async def find_one(self, query):
        return next(
            (doc.copy() for doc in self.documents if all(
                doc.get(key) == value for key, value in query.items()
            )),
            None,
        )

    def find(self, query=None, projection=None):
        query = query or {}
        return _Cursor([
            doc.copy() for doc in self.documents
            if all(doc.get(key) == value for key, value in query.items())
        ])

    async def insert_one(self, document):
        stored = document.copy()
        stored["_id"] = len(self.documents) + 1
        self.documents.append(stored)
        return SimpleNamespace(inserted_id=stored["_id"])

    async def update_one(self, query, update):
        for document in self.documents:
            if not all(document.get(key) == value for key, value in query.items()):
                continue
            document.update(update.get("$set", {}))
            for key in update.get("$unset", {}):
                document.pop(key, None)
            return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def update_many(self, query, update):
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
        self.key = Fernet.generate_key().decode("ascii")
        self.manager = AccountManager()
        self.raw_db = SimpleNamespace(accounts=_AccountCollection())
        self.manager_patch = patch("core.accounts.get_raw_db", return_value=self.raw_db)
        self.manager_patch.start()
        self.key_patch = patch("core.accounts.SESSION_ENCRYPTION_KEY", self.key)
        self.key_patch.start()
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
        self.key_patch.stop()
        self.client_patch.stop()

    async def test_accounts_encrypt_sessions_select_independently_and_remove_one(self):
        first = await self.manager.add_session("pyrogram-session-101")
        second = await self.manager.add_session("pyrogram-session-202")

        self.assertEqual(self.manager.selected_account_id, 202)
        self.assertTrue(self.clients[101].is_connected)
        self.assertTrue(self.clients[202].is_connected)
        saved = self.raw_db.accounts.documents
        self.assertEqual({doc["account_id"] for doc in saved}, {101, 202})
        self.assertNotIn("pyrogram-session-101", saved[0]["encrypted_session"])
        self.assertEqual(self.manager._decrypt(saved[0]["encrypted_session"]), "pyrogram-session-101")

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
        self.assertNotIn("encrypted_session", saved[0])

    async def test_duplicate_identity_is_rejected_even_with_a_different_session_string(self):
        await self.manager.add_session("pyrogram-session-101")
        with self.assertRaises(AccountError) as raised:
            await self.manager.add_session("different-session-101")
        self.assertIn("already connected", str(raised.exception))
        self.assertEqual(len(self.raw_db.accounts.documents), 1)
        self.assertTrue(self.manager.clients[101].is_connected)

    async def test_startup_reloads_each_encrypted_account_without_sharing_clients(self):
        for account_id in (101, 202):
            encrypted = self.manager._encrypt(f"pyrogram-session-{account_id}")
            self.raw_db.accounts.documents.append({
                "_id": account_id,
                "owner_id": 1,
                "account_id": account_id,
                "telegram_user_id": account_id,
                "encrypted_session": encrypted,
                "active": True,
                "selected": account_id == 202,
                "created_at": account_id,
            })

        await self.manager.startup()

        self.assertEqual(set(self.manager.clients), {101, 202})
        self.assertEqual(self.manager.selected_account_id, 202)
        self.assertTrue(all(client.is_connected for client in self.manager.clients.values()))

    async def test_invalid_session_is_reported_without_echoing_credentials(self):
        self.client_patch.stop()
        self.client_patch = patch(
            "core.accounts.create_user_client",
            return_value=_FakeClient(0, SessionRevoked("sensitive-session-value")),
        )
        self.client_patch.start()
        with self.assertRaises(AccountError) as raised:
            await self.manager.add_session("sensitive-session-value")
        self.assertNotIn("sensitive-session-value", str(raised.exception))
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
