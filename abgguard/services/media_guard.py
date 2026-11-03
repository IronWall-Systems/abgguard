from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import Message

from abgguard.core import redis as rk
from abgguard.services.antiporn import porn_client
from abgguard.services.moderation import delete_and_ban, safe_delete

log = logging.getLogger("abgguard.media")

MAX_SCAN_BYTES = 20 * 1024 * 1024
SCAN_KINDS = {"photo", "video", "gif", "video_note", "document", "sticker"}
PUBLIC_PORN_NOTICE = (
    "🚫 ارسال محتوای مستهجن در این گروه ممنوع است؛ پیام حذف و کاربر سکوت شد."
)
PORN_REASON = "permamute:ارسال محتوای مستهجن (Pornographic Media)"
HIT_TTL = 7 * 24 * 3600


def payload_of(message: Message):
    if message.photo:
        return message.photo[-1], "image/jpeg", "photo"
    if message.animation:
        return message.animation, message.animation.mime_type or "image/gif", "gif"
    if message.video:
        return message.video, message.video.mime_type or "video/mp4", "video"
    if message.video_note:
        return message.video_note, "video/mp4", "video_note"
    if message.document:
        mime = (message.document.mime_type or "").lower()
        if not mime.startswith(("image/", "video/")):
            return None, "", "document"
        return message.document, mime, "document"
    if message.sticker:
        sticker = message.sticker
        if sticker.is_animated or sticker.is_video:
            thumb = sticker.thumbnail
            if thumb is None:
                return None, "", "sticker"
            return thumb, "image/webp", "sticker"
        return sticker, "image/webp", "sticker"
    return None, "", "other"


def _thumbnail_of(message: Message):
    for attr in ("video", "animation", "video_note", "document"):
        obj = getattr(message, attr, None)
        thumb = getattr(obj, "thumbnail", None) if obj is not None else None
        if thumb is not None:
            return thumb
    return None


async def scan_media(bot: Bot, message: Message) -> tuple[bool, float]:
    source, content_type, kind = payload_of(message)
    if source is None:
        return False, 0.0
    size = getattr(source, "file_size", None) or 0
    if size > MAX_SCAN_BYTES:
        thumb = _thumbnail_of(message)
        if thumb is None:
            log.info(
                "media too large and has no thumbnail chat=%s msg=%s kind=%s size=%s",
                message.chat.id,
                message.message_id,
                kind,
                size,
            )
            return False, 0.0
        log.info(
            "large media - scanning thumbnail chat=%s msg=%s kind=%s size=%s",
            message.chat.id,
            message.message_id,
            kind,
            size,
        )
        source = thumb
        content_type = "image/jpeg"

    log.info(
        "media scan start chat=%s msg=%s kind=%s size=%s",
        message.chat.id,
        message.message_id,
        kind,
        getattr(source, "file_size", None) or 0,
    )
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
        log.warning(
            "media scan unavailable chat=%s msg=%s kind=%s",
            message.chat.id,
            message.message_id,
            kind,
        )
        return False, verdict.score
    if verdict.is_porn:
        log.info(
            "porn hit chat=%s msg=%s score=%.2f labels=%s",
            message.chat.id,
            message.message_id,
            verdict.score,
            verdict.labels,
        )
    else:
        log.info(
            "media clean chat=%s msg=%s score=%.2f labels=%s",
            message.chat.id,
            message.message_id,
            verdict.score,
            verdict.labels,
        )
    return verdict.is_porn, verdict.score


async def _claim_hit(chat_id: int, message_id: int) -> bool:
    key = f"{rk.P}:porn:hit:{chat_id}:{message_id}"
    try:
        return bool(
            await rk.get_redis().set(key, "1", ex=HIT_TTL, nx=True)
        )
    except Exception:
        return True


async def punish_porn(
    bot: Bot,
    message: Message,
    score: float,
    *,
    actor_source: str,
    enforce: bool = True,
) -> None:
    user = message.from_user
    if user is None:
        return
    chat_id = message.chat.id
    if not await _claim_hit(chat_id, message.message_id):
        return

    if not enforce:
        await safe_delete(bot, chat_id, message.message_id)
        log.info(
            "porn hit by staff/VIP chat=%s user=%s msg=%s - delete only",
            chat_id,
            user.id,
            message.message_id,
        )
        return

    try:
        member = await bot.get_chat_member(chat_id, user.id)
        if str(getattr(member, "status", "")).lower() in {"administrator", "creator"}:
            await safe_delete(bot, chat_id, message.message_id)
            log.info(
                "porn hit by chat admin chat=%s user=%s msg=%s - delete only",
                chat_id,
                user.id,
                message.message_id,
            )
            return
    except Exception:
        log.debug("chat member lookup failed chat=%s user=%s", chat_id, user.id)

    await delete_and_ban(
        bot,
        chat_id=chat_id,
        user_id=user.id,
        message_id=message.message_id,
        reason=PORN_REASON,
        target_name=user.full_name,
        score=score,
        actor_source=actor_source,
        notify=PUBLIC_PORN_NOTICE,
    )
