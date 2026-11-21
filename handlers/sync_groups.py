from pyrogram.enums import ChatType
from pyrogram.handlers import ChatMemberUpdatedHandler
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

from database import get_db
from core.account_context import account_context
from core.userbot import userbot
from handlers.admin import owner_only

_MEMBERSHIP_INACTIVE_PREFIX = "membership:"


def _is_membership_inactive(reason) -> bool:
    reason = str(reason or "")
    return reason.startswith(_MEMBERSHIP_INACTIVE_PREFIX) or reason.startswith(
        (
            "UserNotParticipant",
            "UserBannedInChannel",
            "ChannelPrivate",
            "PeerIdInvalid",
            "ChatWriteForbidden",
            "ChatAdminRequired",
        )
    )


async def _store_joined_group(chat) -> bool:
    db = get_db()
    values = {
        "chat_id": chat.id,
        "title": chat.title or "",
        "username": chat.username or "",
        "active": True,
        "membership_status": "member",
        "promotion_authorized": True,
    }
    result = await db.groups.update_one(
        {"chat_id": chat.id},
        {"$set": values},
        upsert=True,
    )
    inactive = await db.inactive_groups.find_one({"chat_id": chat.id})
    if inactive and _is_membership_inactive(inactive.get("reason")):
        await db.inactive_groups.delete_one({"chat_id": chat.id})
    return result.upserted_id is not None


async def mark_membership_inactive(chat_id: int, reason: str) -> None:
    db = get_db()
    inactive_reason = f"{_MEMBERSHIP_INACTIVE_PREFIX}{reason}"
    group = await db.groups.find_one({"chat_id": chat_id})
    inactive = await db.inactive_groups.find_one({"chat_id": chat_id})
    if (
        group
        and group.get("active") is False
        and inactive
        and inactive.get("reason") == inactive_reason
    ):
        return
    normalized_reason = reason.lower()
    membership_status = (
        "banned" if "banned" in normalized_reason
        else "left" if "left" in normalized_reason or "not_present" in normalized_reason
        else "not_member"
    )
    await db.groups.update_one(
        {"chat_id": chat_id},
        {
            "$set": {
                "chat_id": chat_id,
                "active": False,
                "membership_status": membership_status,
            },
            "$setOnInsert": {"promotion_authorized": True},
        },
        upsert=True,
    )
    from handlers.inactive_groups import mark_inactive

    await mark_inactive(chat_id, inactive_reason)


def register_membership_updates(client, account_id: int) -> None:
    """Attach account-bound membership updates to a connected Pyrogram client."""
    if getattr(client, "_membership_updates_registered", False):
        return

    async def on_membership_update(_client, update) -> None:
        member = update.new_chat_member
        member_user = getattr(member, "user", None)
        if not member_user or int(member_user.id) != int(account_id):
            return
        if update.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            return

        with account_context(account_id):
            status = getattr(member.status, "value", member.status)
            status = str(status).upper().rsplit(".", 1)[-1]
            if status in {"LEFT", "BANNED"} or (
                status == "RESTRICTED"
                and getattr(member, "is_member", True) is False
            ):
                await mark_membership_inactive(update.chat.id, status.lower())
            else:
                await _store_joined_group(update.chat)

    client.add_handler(ChatMemberUpdatedHandler(on_membership_update))
    client._membership_updates_registered = True


async def sync_joined_groups() -> tuple[int, int, int, int, int]:
    """
    Iterate every userbot dialog and upsert groups/supergroups into MongoDB.

    For each group/supergroup found accessible:
      - If new → insert (added count goes up).
      - If already stored → update title and username (updated count goes up).
      - Membership-related inactive records are cleared only after rejoining.
      - Previously stored groups missing from the completed scan are marked inactive.
    Private chats, bots, and channels are silently skipped.

    Returns:
        (total_scanned, added, updated, skipped, reactivated)
        total_scanned — total dialogs iterated
        added         — groups newly inserted into DB
        updated       — groups already in DB (title/username refreshed)
        skipped       — dialogs that are not a group or supergroup
        reactivated   — groups cleared from membership-related inactivity
    """
    db = get_db()

    total_scanned = 0
    added = 0
    updated = 0
    skipped = 0
    reactivated = 0

    joined_chats = []
    async for dialog in userbot.get_dialogs():
        total_scanned += 1
        chat = dialog.chat

        if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            skipped += 1
            continue
        joined_chats.append(chat)

    # Do not mark groups missing until the complete dialog scan succeeds.
    for chat in joined_chats:
        inactive = await db.inactive_groups.find_one({"chat_id": chat.id})
        if await _store_joined_group(chat):
            added += 1
        else:
            updated += 1
        if inactive and _is_membership_inactive(inactive.get("reason")):
            reactivated += 1
            print(f"   ♻️  Reactivated membership for group {chat.id} ({chat.title})")

    joined_ids = {chat.id for chat in joined_chats}
    stored_groups = await db.groups.find({}, {"chat_id": 1}).to_list(None)
    for group in stored_groups:
        chat_id = group["chat_id"]
        if chat_id not in joined_ids:
            await mark_membership_inactive(chat_id, "not_present_in_dialogs")

    print(
        f"🔄 Sync complete — scanned: {total_scanned}, "
        f"added: {added}, updated: {updated}, "
        f"skipped: {skipped}, reactivated: {reactivated}"
    )
    return total_scanned, added, updated, skipped, reactivated


# ── Command handler ───────────────────────────────────────────────────────────

@owner_only
async def cmd_syncgroups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/syncgroups — Re-scan all joined groups, update the database, and
    reactivate membership-related inactive groups and retain departed records."""
    await update.message.reply_text("🔄 Syncing joined groups, please wait…")

    try:
        total, added, updated, skipped, reactivated = await sync_joined_groups()
    except Exception as e:
        await update.message.reply_text(f"❌ Sync failed: {e}")
        return

    text = (
        "✅ Group Sync Complete\n\n"
        f"Groups Scanned: {total}\n"
        f"Added: {added}\n"
        f"Updated: {updated}\n"
        f"Reactivated: {reactivated}\n"
        f"Skipped: {skipped}"
    )
    await update.message.reply_text(text)


# ── Registration ──────────────────────────────────────────────────────────────

def register(app):
    app.add_handler(CommandHandler("syncgroups", cmd_syncgroups))
