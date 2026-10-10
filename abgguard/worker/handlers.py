from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.types import Message

from abgguard.core import rbac, settings_store
from abgguard.core.rbac import Role
from abgguard.services.antiporn import porn_client
from abgguard.services.moderation import delete_and_ban, delete_and_mute, safe_delete
from abgguard.services.textguards import (
    find_link,
    is_bot_message,
    is_forwarded,
    is_user_vip,
)

log = logging.getLogger("abgguard.worker")

router = Router(name="worker.media")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))

MEDIA = F.photo | F.video | F.animation | F.video_note | F.document


def _payload_of(message: Message):
    if message.photo:
        return message.photo[-1], "image/jpeg", "photo"
    if message.animation:
        return message.animation, "image/gif", "gif"
    if message.video:
        return message.video, "video/mp4", "video"
    if message.video_note:
        return message.video_note, "video/mp4", "video_note"
    if message.document:
        mime = (message.document.mime_type or "").lower()
        if not mime.startswith(("image/", "video/")):
            return None, "", "document"
        return message.document, mime, "document"
    if message.sticker:
        return None, "", "sticker"
    return None, "", "other"


async def _scan(bot: Bot, message: Message) -> tuple[bool, float]:
    source, content_type, _kind = _payload_of(message)
    if source is None:
        return False, 0.0
    if content_type == "video/mp4" and (source.file_size or 0) > 18 * 1024 * 1024:
        log.debug("skip oversized video scan chat=%s", message.chat.id)
        return False, 0.0

    try:
        stream = await bot.download(source, destination=None)
    except Exception as exc:
        log.warning("download failed chat=%s: %s", message.chat.id, exc)
        return False, 0.0
    if stream is None:
        return False, 0.0

    payload = stream.read()
    stream.close()
    verdict = await porn_client.scan_bytes(payload, content_type or "image/jpeg")
    if not verdict.available:
        return False, verdict.score
    if verdict.is_porn:
        log.info(
            "porn hit chat=%s msg=%s score=%.2f labels=%s",
            message.chat.id, message.message_id, verdict.score, verdict.labels,
        )
    return verdict.is_porn, verdict.score


@router.message(MEDIA)
async def on_media(message: Message, bot: Bot) -> None:
    user = message.from_user
    chat_id = message.chat.id
    if is_bot_message(message):
        # the watchdog never acts on a bot's own output (master panel, notices)
        return

    role = await rbac.get_role(chat_id, user.id)

    # ---- temporary VIP (3h): the watchdog never deletes their media -------
    if role < Role.SUBADMIN and await is_user_vip(chat_id, user.id):
        return

    flags = await settings_store.get_flags(chat_id)

    # ---- forward lock applies to media too --------------------------------
    if flags.forward_lock and is_forwarded(message) and role < Role.SUBADMIN:
        await safe_delete(bot, chat_id, message.message_id)
        return

    # ---- caption guards (the master never sees media captions) -----------
    if role < Role.SUBADMIN and message.caption:
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
            await delete_and_mute(
                bot,
                chat_id=chat_id,
                user_id=user.id,
                message_id=message.message_id,
                seconds=600,
                reason=f"کلمه رکیک ({hit})",
                actor_source="watchdog",
            )
            return

    if role >= Role.SUBADMIN:
        return  # staff media is never blocked

    _, _, kind = _payload_of(message)

    # ---- media type locks -------------------------------------------------
    if kind == "photo" and flags.photo_lock:
        await safe_delete(bot, chat_id, message.message_id)
        return
    if kind == "video" and flags.video_lock:
        await safe_delete(bot, chat_id, message.message_id)
        return
    if kind == "gif" and flags.gif_lock:
        await safe_delete(bot, chat_id, message.message_id)
        return
    if kind == "sticker" and flags.photo_lock:
        # stickers travel as images in most lock policies
        await safe_delete(bot, chat_id, message.message_id)
        return

    # ---- local AI anti-porn ------------------------------------------------
    if flags.porn_lock and kind in {"photo", "video", "gif", "video_note", "document"}:
        is_porn, score = await _scan(bot, message)
        if is_porn:
            # no card in the group: the media is deleted here and the sanction
            # (permanent mute) + the private report land in the admins' PVs.
            # INVARIANT: this path never bans. `reason` starts with the
            # `permamute:` marker, so `moderation.handle_ban_event()` applies
            # `restrict_chat_member(can_send_messages=False)` forever and
            # escalates to the Level 2 owner - `ban_chat_member` is only ever
            # reached by an explicit admin command in `master/handlers/modcmd.py`.
            await delete_and_ban(
                bot,
                chat_id=chat_id,
                user_id=user.id,
                message_id=message.message_id,
                reason="permamute:ارسال محتوای مستهجن (Pornographic Media)",
                target_name=user.full_name,
                score=score,
                actor_source="watchdog",
            )


@router.message(F.chat.type.in_({"group", "supergroup"}), F.sticker)
async def on_sticker(message: Message, bot: Bot) -> None:
    """Stickers arrive with no lock of their own - only the emoji lock matters."""
    user = message.from_user
    if is_bot_message(message):
        return
    chat_id = message.chat.id
    if await rbac.get_role(chat_id, user.id) >= Role.SUBADMIN:
        return
    flags = await settings_store.get_flags(chat_id)
    if flags.emoji_lock:
        await safe_delete(bot, chat_id, message.message_id)
