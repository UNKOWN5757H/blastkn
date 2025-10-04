from pyrogram import Client, filters
from pyrogram.types import ChatJoinRequest, ChatMemberUpdated
from pyrogram.enums import ChatMemberStatus
import asyncio
import time

# =============================================================== #
# Simple in-memory debounce store
# Format: {(user_id, channel_id, action): last_timestamp}
# =============================================================== #
DEBOUNCE_CACHE = {}
DEBOUNCE_TTL = 10  # seconds (ignore duplicate updates within this window)


def is_duplicate(user_id: int, channel_id: int, action: str) -> bool:
    """Check and store if an action was recently processed."""
    now = time.time()
    key = (user_id, channel_id, action)
    last_time = DEBOUNCE_CACHE.get(key, 0)
    if now - last_time < DEBOUNCE_TTL:
        return True  # duplicate
    DEBOUNCE_CACHE[key] = now
    # Clean up old entries occasionally
    if len(DEBOUNCE_CACHE) > 10000:
        for k in list(DEBOUNCE_CACHE.keys()):
            if now - DEBOUNCE_CACHE[k] > DEBOUNCE_TTL:
                del DEBOUNCE_CACHE[k]
    return False


# =============================================================== #
# JOIN REQUEST HANDLER
# =============================================================== #

@Client.on_chat_join_request(filters.channel)
async def handle_join_request(client, join_request: ChatJoinRequest):
    user_id = join_request.from_user.id
    channel_id = join_request.chat.id
    channel_name = join_request.chat.title or "Unknown Channel"
    channel = client.fsub_dict.get(channel_id, [])

    is_banned = await client.mongodb.is_banned(user_id)
    if is_banned:
        return

    if channel:
        join_request_id = getattr(join_request, "id", None)
        await client.mongodb.add_join_request(user_id, channel_id, join_request_id)
        await client.mongodb.update_fsub_status(user_id, channel_id, "request_submitted")
        await client.mongodb.add_channel_user(channel_id, user_id)

        client.LOGGER(__name__, client.name).info(
            f"[JOIN REQUEST] User {user_id} requested to join {channel_name}"
        )


# =============================================================== #
# MEMBER UPDATE HANDLER WITH DEBOUNCE
# =============================================================== #

@Client.on_chat_member_updated(filters.channel)
async def handle_member_update(client, chat_member_updated: ChatMemberUpdated):
    """Handle when users join, leave, or get banned from channels"""
    try:
        chat = chat_member_updated.chat
        channel_id = chat.id
        channel_name = getattr(chat, "title", "Unknown Channel")

        if channel_id not in client.fsub_dict:
            return

        user_id = getattr(chat_member_updated.from_user, "id", None)
        if not user_id:
            return  # skip updates without user info

        old_status = getattr(chat_member_updated.old_chat_member, "status", None)
        new_status = getattr(chat_member_updated.new_chat_member, "status", None)

        if not new_status and not old_status:
            client.LOGGER(__name__, client.name).warning(
                f"[SKIP] No status data for user {user_id} in {channel_name}"
            )
            return

        # ---- JOINED ----
        if new_status in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}:
            if is_duplicate(user_id, channel_id, "joined"):
                return  # prevent duplicate
            await client.mongodb.update_fsub_status(user_id, channel_id, "joined")
            await client.mongodb.add_channel_user(channel_id, user_id)

            if await client.mongodb.has_submitted_join_request(user_id, channel_id):
                await client.mongodb.update_join_request_status(user_id, channel_id, "approved")

            client.LOGGER(__name__, client.name).info(
                f"[JOINED] User {user_id} joined channel {channel_name}"
            )

        # ---- LEFT / REMOVED ----
        elif old_status in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER} and new_status in {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED, None}:
            if is_duplicate(user_id, channel_id, "left"):
                return  # prevent duplicate
            await client.mongodb.update_fsub_status(user_id, channel_id, "left")
            await client.mongodb.remove_channel_user(channel_id, user_id)

            if await client.mongodb.has_submitted_join_request(user_id, channel_id):
                await client.mongodb.remove_join_request(user_id, channel_id)

            client.LOGGER(__name__, client.name).info(
                f"[LEFT] User {user_id} left or was removed from {channel_name}"
            )

        # ---- BANNED ----
        elif new_status == ChatMemberStatus.BANNED:
            if is_duplicate(user_id, channel_id, "banned"):
                return  # prevent duplicate
            await client.mongodb.update_fsub_status(user_id, channel_id, "banned")
            await client.mongodb.remove_channel_user(channel_id, user_id)

            if await client.mongodb.has_submitted_join_request(user_id, channel_id):
                await client.mongodb.remove_join_request(user_id, channel_id)

            client.LOGGER(__name__, client.name).info(
                f"[BANNED] User {user_id} banned from channel {channel_name}"
            )

    except Exception as e:
        client.LOGGER(__name__, client.name).error(
            f"[ERROR] handle_member_update: {e}", exc_info=True
        )
