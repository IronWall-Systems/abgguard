from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.types import Message

from abgguard.core import rbac, settings_store
from abgguard.core.rbac import Role
from abgguard.services import subscription
from abgguard.services.media_guard import (
    SCAN_KINDS,
    payload_of,
    punish_porn,
    scan_media,
)
from abgguard.services.moderation import (
    delete_and_mute,
    escalate_content_violation,
    safe_delete,
)
from abgguard.services.textguards import (
    find_link,
    is_bot_message,
    is_forwarded,
    is_user_vip,
)

log = logging.getLogger("abgguard.worker")

router = Router(name="worker.media")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))

MEDIA = F.photo | F.video | F.animation | F.video_note | F.document | F.sticker


@router.message(MEDIA)
async def on_media(message: Message, bot: Bot) -> None:
    user = message.from_user
    chat_id = message.chat.id
    if is_bot_message(message):
        # the watchdog never acts on a bot's own output (master panel, notices)
        return

    # SaaS gate: UNPAID / EXPIRED chats get no media/porn/caption scanning.
    if not await subscription.is_active(chat_id):
        return

    role = await rbac.get_role(chat_id, user.id)
    is_vip = role < Role.SUBADMIN and await is_user_vip(chat_id, user.id)

    flags = await settings_store.get_flags(chat_id)

    # ---- forward lock applies to media too --------------------------------
    if flags.forward_lock and is_forwarded(message) and role < Role.SUBADMIN and not is_vip:
        await safe_delete(bot, chat_id, message.message_id)
        return

    # ---- caption guards (the master never sees media captions) -----------
    if not is_vip and role < Role.SUBADMIN and message.caption:
        if flags.link_lock and find_link(message.caption):
            await delete_and_mute(
                bot,
                chat_id=chat_id,
                user_id=user.id,
                message_id=message.message_id,
                seconds=300,
                reason="قفل لینک (کپشن)",
                actor_source="watchdog",
            )
            return
        hit = await settings_store.contains_badword(chat_id, message.caption)
        if hit:
            await escalate_content_violation(
                bot,
                chat_id=chat_id,
                user_id=user.id,
                message_id=message.message_id,
                reason="کلمه رکیک",
                log_reason=f"کلمه رکیک ({hit})",
                actor_source="watchdog",
                target_name=user.full_name,
                target_username=user.username,
                content_report=True,
                message_text=message.caption,
            )
            return

    _, _, kind = payload_of(message)
    is_exempt = role >= Role.SUBADMIN or is_vip

    # ---- media type locks -------------------------------------------------
    if not is_exempt:
        if kind == "photo" and flags.photo_lock:
            await safe_delete(bot, chat_id, message.message_id)
            return
        if kind == "video" and flags.video_lock:
            await safe_delete(bot, chat_id, message.message_id)
            return
        if kind == "gif" and flags.gif_lock:
            await safe_delete(bot, chat_id, message.message_id)
            return
        if kind == "sticker" and (flags.emoji_lock or flags.photo_lock):
            await safe_delete(bot, chat_id, message.message_id)
            return

    # ---- local AI anti-porn ------------------------------------------------
    if flags.porn_lock and kind in SCAN_KINDS:
        is_porn, score = await scan_media(bot, message)
        if is_porn:
            await punish_porn(
                bot,
                message,
                score,
                actor_source="watchdog",
                enforce=not is_exempt,
            )
