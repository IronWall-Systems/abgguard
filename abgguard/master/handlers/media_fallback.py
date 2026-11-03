from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import Message

from abgguard.core import botpair, rbac, settings_store
from abgguard.core.rbac import Role
from abgguard.services import subscription
from abgguard.services.media_guard import (
    SCAN_KINDS,
    payload_of,
    punish_porn,
    scan_media,
)
from abgguard.services.textguards import is_bot_message, is_user_vip

router = Router(name="master.media_fallback")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))

MEDIA = F.photo | F.video | F.animation | F.video_note | F.document | F.sticker


@router.message(MEDIA)
async def on_media_fallback(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None or is_bot_message(message):
        return
    chat_id = message.chat.id
    if await botpair.worker_guard_ready(chat_id):
        return
    if not await subscription.is_active(chat_id):
        return
    role = await rbac.get_role(chat_id, user.id)
    is_exempt = role >= Role.SUBADMIN or await is_user_vip(chat_id, user.id)
    flags = await settings_store.get_flags(chat_id)
    if not flags.porn_lock:
        return
    _, _, kind = payload_of(message)
    if kind not in SCAN_KINDS:
        return
    is_porn, score = await scan_media(bot, message)
    if is_porn:
        await punish_porn(
            bot,
            message,
            score,
            actor_source="master",
            enforce=not is_exempt,
        )
