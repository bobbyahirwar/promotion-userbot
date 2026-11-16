import sys
from copy import deepcopy
import motor.motor_asyncio
from config import MONGO_URI
from core.account_context import current_account_id

_client: motor.motor_asyncio.AsyncIOMotorClient = None
db = None

_ACCOUNT_COLLECTIONS = {
    "groups": ("account_groups", None),
    "messages": ("account_messages", None),
    "inactive_groups": ("account_inactive_groups", None),
    "blacklist": ("account_blacklist", None),
    "autorotate_configs": ("account_autorotate", None),
    "state": ("account_settings", "state"),
    "stats": ("account_settings", "stats"),
    "promotion_logs": ("account_logs", "log"),
    "promotion_reports": ("account_logs", "report"),
}


async def init_db():
    global _client, db
    try:
        _client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
        # Verify the connection is reachable
        await _client.admin.command("ping")
        db = _client.get_default_database(default="promotionbot")
        await ensure_account_indexes()
        print("✅ MongoDB Connected")
    except Exception as e:
        print(f"❌ MongoDB failed to connect: {e}")
        sys.exit(1)


def get_db(account_id: int | None = None):
    account_id = account_id if account_id is not None else current_account_id()
    if account_id is None:
        from core.accounts import account_manager

        account_id = account_manager.selected_account_id
    if account_id is None:
        raise RuntimeError("No Telegram account is selected.")
    if db is None:
        raise RuntimeError("MongoDB has not been initialized.")
    return AccountDatabase(db, account_id)


def get_raw_db():
    if db is None:
        raise RuntimeError("MongoDB has not been initialized.")
    return db


async def ensure_account_indexes():
    """Create indexes only on the new multi-account collections."""
    for collection, keys, unique in (
        ("accounts", [("account_id", 1)], True),
        ("account_groups", [("account_id", 1), ("chat_id", 1)], True),
        ("account_messages", [("account_id", 1), ("_id", 1)], False),
        ("account_inactive_groups", [("account_id", 1), ("chat_id", 1)], True),
        ("account_blacklist", [("account_id", 1), ("chat_id", 1)], True),
        ("account_autorotate", [("account_id", 1), ("_id", 1)], False),
        ("account_settings", [("account_id", 1), ("kind", 1), ("key", 1)], True),
        ("account_logs", [("account_id", 1), ("kind", 1), ("config_id", 1)], False),
        ("account_logs", [("account_id", 1), ("kind", 1), ("timestamp", -1)], False),
    ):
        await db[collection].create_index(keys, unique=unique)
    await db.accounts.create_index("telegram_user_id", unique=True)
    await db.accounts.create_index(
        [("active", 1), ("selected", 1)],
        name="active_selected_account",
    )


class _ScopedCollection:
    def __init__(self, collection, account_id: int, kind: str | None):
        self._collection = collection
        self._account_id = int(account_id)
        self._kind = kind

    def _query(self, query=None):
        scope = {"account_id": self._account_id}
        if self._kind:
            scope["kind"] = self._kind
        query = deepcopy(query or {})
        if not query:
            return scope
        if not set(query).intersection(scope):
            return {**query, **scope}
        if all(query.get(key) == value for key, value in scope.items()):
            return query
        return {"$and": [query, scope]}

    def _document(self, document):
        result = deepcopy(document)
        result["account_id"] = self._account_id
        if self._kind:
            result["kind"] = self._kind
        return result

    def _update(self, update):
        result = deepcopy(update)
        allowed = {"$set", "$unset", "$addToSet", "$inc", "$setOnInsert"}
        if any(operation not in allowed for operation in result):
            raise ValueError("Unsupported account-scoped update operation.")
        protected = {"account_id"}
        if self._kind:
            protected.add("kind")
        for operation, fields in result.items():
            if operation.startswith("$") and isinstance(fields, dict):
                if any(
                    field == scope_field or field.startswith(f"{scope_field}.")
                    for field in fields
                    for scope_field in protected
                ):
                    raise ValueError("Account scope fields cannot be modified.")
        result.setdefault("$setOnInsert", {})["account_id"] = self._account_id
        if self._kind:
            result["$setOnInsert"]["kind"] = self._kind
        return result

    async def insert_one(self, document, *args, **kwargs):
        return await self._collection.insert_one(self._document(document), *args, **kwargs)

    async def insert_many(self, documents, *args, **kwargs):
        return await self._collection.insert_many(
            [self._document(document) for document in documents], *args, **kwargs
        )

    def find(self, query=None, *args, **kwargs):
        return self._collection.find(self._query(query), *args, **kwargs)

    async def find_one(self, query=None, *args, **kwargs):
        return await self._collection.find_one(self._query(query), *args, **kwargs)

    async def count_documents(self, query=None, *args, **kwargs):
        return await self._collection.count_documents(self._query(query), *args, **kwargs)

    async def update_one(self, query, update, *args, **kwargs):
        return await self._collection.update_one(
            self._query(query), self._update(update), *args, **kwargs
        )

    async def update_many(self, query, update, *args, **kwargs):
        return await self._collection.update_many(
            self._query(query), self._update(update), *args, **kwargs
        )

    async def delete_one(self, query, *args, **kwargs):
        return await self._collection.delete_one(self._query(query), *args, **kwargs)

    async def delete_many(self, query, *args, **kwargs):
        return await self._collection.delete_many(self._query(query), *args, **kwargs)


class AccountDatabase:
    def __init__(self, database, account_id: int):
        self._database = database
        self._account_id = int(account_id)

    def __getattr__(self, name):
        mapping = _ACCOUNT_COLLECTIONS.get(name)
        if mapping:
            collection_name, kind = mapping
            return _ScopedCollection(
                self._database[collection_name], self._account_id, kind
            )
        return getattr(self._database, name)
