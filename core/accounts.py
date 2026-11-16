import asyncio
import logging
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken
from pyrogram import Client
from pyrogram.errors import (
    AuthKeyDuplicated,
    AuthKeyUnregistered,
    SessionExpired,
    SessionRevoked,
)

from config import API_ID, API_HASH, OWNER_ID, SESSION_ENCRYPTION_KEY, SESSION_STRING
from core.account_context import account_context, current_account_id
from core.userbot import create_user_client
from database import get_raw_db

log = logging.getLogger(__name__)


class AccountError(Exception):
    """A safe, user-facing account operation error."""


class DuplicateAccountError(AccountError):
    pass


class AccountManager:
    def __init__(self):
        self.clients: dict[int, Client] = {}
        self.status: dict[int, str] = {}
        self._removing: set[int] = set()
        self.selected_account_id: int | None = None
        self._cipher: Fernet | None = None
        self._monitor_task: asyncio.Task | None = None

    def _get_cipher(self) -> Fernet:
        if self._cipher is None:
            if not SESSION_ENCRYPTION_KEY:
                raise RuntimeError(
                    "SESSION_ENCRYPTION_KEY must be configured before accounts can start."
                )
            try:
                self._cipher = Fernet(SESSION_ENCRYPTION_KEY.encode("ascii"))
            except (ValueError, UnicodeEncodeError) as exc:
                raise RuntimeError("SESSION_ENCRYPTION_KEY is not a valid Fernet key.") from exc
        return self._cipher

    def _encrypt(self, session_string: str) -> str:
        return self._get_cipher().encrypt(session_string.encode("utf-8")).decode("ascii")

    def _decrypt(self, encrypted_session: str) -> str:
        try:
            return self._get_cipher().decrypt(encrypted_session.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeEncodeError, UnicodeDecodeError) as exc:
            raise AccountError("Stored account credentials could not be decrypted.") from exc

    def get_client(self, account_id: int | None = None) -> Client:
        account_id = account_id or current_account_id() or self.selected_account_id
        if account_id is None:
            raise RuntimeError("No Telegram account is selected.")
        client = self.clients.get(int(account_id))
        if client is None or not client.is_connected:
            raise RuntimeError("The selected Telegram account is disconnected.")
        return client

    async def list_accounts(self) -> list[dict]:
        return await get_raw_db().accounts.find(
            {"owner_id": OWNER_ID, "active": True}, {"encrypted_session": 0}
        ).sort(
            [("created_at", 1), ("account_id", 1)]
        ).to_list(None)

    async def startup(self) -> None:
        self._get_cipher()
        db = get_raw_db()
        records = await db.accounts.find(
            {"owner_id": OWNER_ID, "active": True}
        ).sort("created_at", 1).to_list(None)
        if not records and SESSION_STRING:
            try:
                await self.add_session(SESSION_STRING, select=True)
                log.info("Imported the configured legacy Pyrogram session into accounts.")
            except Exception:
                log.error("The configured legacy Pyrogram session could not be imported.")

        records = await db.accounts.find(
            {"owner_id": OWNER_ID, "active": True}
        ).sort("created_at", 1).to_list(None)
        for record in records:
            account_id = int(record["account_id"])
            if account_id in self.clients and self.clients[account_id].is_connected:
                self.status[account_id] = "Connected"
                continue
            client = None
            try:
                session_string = self._decrypt(record["encrypted_session"])
                client = create_user_client(session_string, f"account-{account_id}")
                await client.start()
                me = await client.get_me()
                if me.id != account_id:
                    await client.stop()
                    self.status[account_id] = "Session identity mismatch"
                    log.error("Account %s failed startup identity verification.", account_id)
                    continue
                self.clients[account_id] = client
                self.status[account_id] = "Connected"
            except Exception:
                self.status[account_id] = "Disconnected"
                log.error("Account %s failed to connect at startup.", account_id)
                if client and client.is_connected:
                    try:
                        await client.stop()
                    except Exception:
                        log.warning("Could not stop a failed account client during startup.")

        selected = next((r for r in records if r.get("selected")), None)
        if selected and int(selected["account_id"]) in self.clients:
            self.selected_account_id = int(selected["account_id"])
        elif self.clients:
            self.selected_account_id = next(iter(self.clients))
            await self.select_account(self.selected_account_id)

        self._monitor_task = asyncio.create_task(
            self._monitor_connections(), name="account-connection-monitor"
        )

    async def add_session(self, session_string: str, select: bool = True) -> dict:
        if not isinstance(session_string, str) or not session_string.strip():
            raise AccountError("Send a Pyrogram StringSession.")
        self._get_cipher()
        client = create_user_client(session_string.strip(), "account-validation")
        registered = False
        account_id = None
        try:
            await client.start()
            me = await client.get_me()
            if getattr(me, "is_bot", False):
                raise AccountError("Only a Telegram user account StringSession can be added.")
            account_id = int(me.id)
            db = get_raw_db()
            existing = await db.accounts.find_one({
                "owner_id": OWNER_ID,
                "account_id": account_id,
            })
            if existing and existing.get("active"):
                raise DuplicateAccountError("This Telegram account is already connected.")

            now = datetime.now(timezone.utc)
            record = {
                "account_id": account_id,
                "telegram_user_id": account_id,
                "owner_id": OWNER_ID,
                "name": " ".join(
                    value for value in (me.first_name, me.last_name) if value
                ) or str(account_id),
                "username": me.username or "",
                "encrypted_session": self._encrypt(session_string.strip()),
                "active": True,
                "selected": False,
                "created_at": (existing or {}).get("created_at", now),
                "updated_at": now,
            }
            if existing:
                await db.accounts.update_one(
                    {"_id": existing["_id"]},
                    {"$set": record},
                )
            else:
                await db.accounts.insert_one(record)
            self.clients[account_id] = client
            self.status[account_id] = "Connected"
            if select or self.selected_account_id is None:
                if not await self.select_account(account_id):
                    raise RuntimeError("Account selection could not be saved.")
            registered = True
            return {
                "account_id": account_id,
                "name": record["name"],
                "username": record["username"],
            }
        except DuplicateAccountError:
            raise
        except AccountError:
            raise
        except (AuthKeyUnregistered, SessionExpired, SessionRevoked, AuthKeyDuplicated) as exc:
            raise AccountError(
                "This session is invalid, expired, unauthorized, or incompatible. "
                "Generate a new Pyrogram 2.0.106 StringSession."
            ) from exc
        except Exception as exc:
            # Do not include the Pyrogram exception text: it can contain sensitive data.
            raise AccountError(
                "Could not validate or save the session. Check its validity, API credentials, "
                "and MongoDB connection."
            ) from exc
        finally:
            if not registered:
                if account_id is not None and self.clients.get(account_id) is client:
                    self.clients.pop(account_id, None)
                    self.status[account_id] = "Disconnected"
            if not registered and client.is_connected:
                try:
                    await client.stop()
                except Exception:
                    log.warning("Could not disconnect a temporary account-validation client.")

    async def select_account(self, account_id: int) -> bool:
        account_id = int(account_id)
        if account_id not in self.clients or not self.clients[account_id].is_connected:
            return False
        db = get_raw_db()
        if not await db.accounts.find_one({
            "owner_id": OWNER_ID,
            "account_id": account_id,
            "active": True,
        }):
            return False
        await db.accounts.update_many(
            {"owner_id": OWNER_ID, "active": True},
            {"$set": {"selected": False}},
        )
        result = await db.accounts.update_one(
            {"owner_id": OWNER_ID, "account_id": account_id, "active": True},
            {"$set": {"selected": True, "updated_at": datetime.now(timezone.utc)}},
        )
        if not result.matched_count:
            return False
        self.selected_account_id = account_id
        return True

    async def remove_account(self, account_id: int) -> bool:
        account_id = int(account_id)
        db = get_raw_db()
        result = await db.accounts.update_one(
            {"owner_id": OWNER_ID, "account_id": account_id, "active": True},
            {
                "$set": {
                    "active": False,
                    "selected": False,
                    "removed_at": datetime.now(timezone.utc),
                },
                "$unset": {"encrypted_session": ""},
            },
        )
        if not result.modified_count:
            return False
        self._removing.add(account_id)
        from core import autorotate, promotion

        try:
            await promotion.stop_promotion(account_id, preserve_state=True)
        except Exception as exc:
            log.error(
                "Could not fully stop promotion for account %s (%s).",
                account_id,
                type(exc).__name__,
            )
        try:
            await autorotate.shutdown_account(account_id)
        except Exception as exc:
            log.error(
                "Could not fully stop AutoRotate for account %s (%s).",
                account_id,
                type(exc).__name__,
            )

        client = self.clients.pop(account_id, None)
        if client and client.is_connected:
            try:
                await client.stop()
            except Exception:
                log.warning("Could not disconnect account %s during removal.", account_id)
        self.status[account_id] = "Disconnected"

        try:
            if self.selected_account_id == account_id:
                self.selected_account_id = None
                available = next(
                    (
                        int(record["account_id"])
                        for record in await self.list_accounts()
                        if int(record["account_id"]) in self.clients
                        and self.clients[int(record["account_id"])].is_connected
                    ),
                    None,
                )
                if available is not None:
                    await self.select_account(available)
        except Exception as exc:
            log.error(
                "Could not select a replacement account after removing %s (%s).",
                account_id,
                type(exc).__name__,
            )
        finally:
            self._removing.discard(account_id)
        return True

    async def reconnect_account(self, account_id: int) -> bool:
        account_id = int(account_id)
        record = await get_raw_db().accounts.find_one(
            {"owner_id": OWNER_ID, "account_id": account_id, "active": True}
        )
        if not record:
            return False
        old_client = self.clients.pop(account_id, None)
        if old_client and old_client.is_connected:
            try:
                await old_client.stop()
            except Exception:
                self.clients[account_id] = old_client
                self.status[account_id] = (
                    "Connected" if old_client.is_connected else "Disconnected"
                )
                log.warning("Could not stop account %s before reconnecting.", account_id)
                return False
        client = None
        try:
            client = create_user_client(
                self._decrypt(record["encrypted_session"]), f"account-{account_id}"
            )
            await client.start()
            me = await client.get_me()
            if me.id != account_id:
                await client.stop()
                self.status[account_id] = "Session identity mismatch"
                return False
            self.clients[account_id] = client
            self.status[account_id] = "Connected"
            return True
        except Exception:
            self.status[account_id] = "Disconnected"
            log.error("Account %s failed to reconnect.", account_id)
            if client and client.is_connected:
                try:
                    await client.stop()
                except Exception:
                    log.warning("Could not clean up failed reconnect for account %s.", account_id)
            return False

    async def _monitor_connections(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                records = await self.list_accounts()
            except Exception:
                log.error("Could not check connected account states.")
                continue
            for record in records:
                account_id = int(record["account_id"])
                if account_id in self._removing:
                    continue
                client = self.clients.get(account_id)
                if client and client.is_connected:
                    self.status[account_id] = "Connected"
                    continue
                self.status[account_id] = "Disconnected"
                try:
                    await self.reconnect_account(account_id)
                except Exception as exc:
                    log.error(
                        "Could not reconnect account %s (%s).",
                        account_id,
                        type(exc).__name__,
                    )

    async def shutdown(self) -> None:
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
        self._monitor_task = None
        for account_id, client in list(self.clients.items()):
            if client.is_connected:
                try:
                    await client.stop()
                except Exception:
                    log.warning("Could not disconnect account %s at shutdown.", account_id)
        self.clients.clear()
        self.status.clear()
        self.selected_account_id = None


account_manager = AccountManager()
