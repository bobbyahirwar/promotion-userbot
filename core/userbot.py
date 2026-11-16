import logging

from pyrogram import Client
from pyrogram.errors import (
    PeerIdInvalid,
    ChannelPrivate,
    UserNotParticipant,
    HistoryGetFailed,
    InternalServerError,
    ChannelInvalid,
)

from config import API_ID, API_HASH

log = logging.getLogger(__name__)


class _SafeClient(Client):
    """Pyrogram Client that silences peer-resolution and transient update sync errors
    inside handle_updates so they can never:
      • kill the asyncio Task that session.py creates for each update batch
      • trigger a MTProto reconnect loop that starves the promotion task

    Background: pyrogram/utils.py:get_peer_type() raises a plain
    ValueError (not a Pyrogram RPC exception) for IDs outside the
    expected ranges. updates.GetChannelDifference can also fail with
    transient 500 server errors (e.g. HistoryGetFailed) during update polling.
    """

    async def handle_updates(self, updates):
        try:
            await super().handle_updates(updates)
        except (ValueError, PeerIdInvalid, ChannelPrivate, UserNotParticipant) as e:
            log.warning("handle_updates: suppressed peer error (%s: %s)",
                        type(e).__name__, e)
        except (HistoryGetFailed, InternalServerError, ChannelInvalid) as e:
            log.warning("handle_updates: suppressed transient update sync error (%s: %s)",
                        type(e).__name__, e)
        # All other exceptions propagate normally so real errors are not hidden.


def create_user_client(session_string: str, name: str) -> Client:
    return _SafeClient(
        name=name,
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=session_string,
    )


class _UserbotRouter:
    """Compatibility router that resolves the current account's client per call."""

    def __getattr__(self, name):
        from core.accounts import account_manager

        from core.account_context import current_account_id

        return getattr(
            account_manager.get_client(current_account_id()), name
        )


userbot = _UserbotRouter()
