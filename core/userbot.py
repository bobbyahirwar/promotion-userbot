import logging

from pyrogram import Client
from pyrogram.errors import (
    PeerIdInvalid,
    ChannelPrivate,
    UserNotParticipant,
)

from config import API_ID, API_HASH, SESSION_STRING

log = logging.getLogger(__name__)


class _SafeClient(Client):
    """Pyrogram Client that silences peer-resolution errors inside
    handle_updates so they can never:
      • kill the asyncio Task that session.py creates for each update batch
      • trigger a MTProto reconnect loop that starves the promotion task

    Background: pyrogram/utils.py:get_peer_type() raises a plain
    ValueError (not a Pyrogram RPC exception) for IDs outside the
    expected ranges.  handle_updates only catches ChannelPrivate, so
    any other peer error escapes and crashes the task silently.
    """

    async def handle_updates(self, updates):
        try:
            await super().handle_updates(updates)
        except (ValueError, PeerIdInvalid, ChannelPrivate, UserNotParticipant) as e:
            log.warning("handle_updates: suppressed peer error (%s: %s)",
                        type(e).__name__, e)
        # All other exceptions propagate normally so real errors are not hidden.


userbot = _SafeClient(
    name="userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)
