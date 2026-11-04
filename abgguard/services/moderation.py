from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ChatPermissions

from abgguard.config import settings
from abgguard.core import redis as rk
from abgguard.models.entities import BanLog
from abgguard.services import modlog

log = logging.getLogger("abgguard.moderation")

# "mute:<reason>:<seconds>s" as written by delete_and_mute()
MUTE_REASON = re.compile(r"^mute:(?P<reason>.*):(?P<seconds>\d+)s$")
# "permamute:<reason>" - a permanent restriction instead of an outright ban
PERMA_REASON = re.compile(r"^permamute:(?P<reason>.+)$")
# Public warnings must only ever show the label, never the matched word.
_BADWORD_PUBLIC = re.compile(r"کلمه رکیک\s*\([^)]*\)")


def redact_public_reason(text: str) -> str:
    return _BADWORD_PUBLIC.sub("کلمه رکیک", text)

NO_SEND = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
    can_change_info=False,
    can_invite_users=False,
    can_pin_messages=False,
)

# strong references so fire-and-forget restorations are never garbage collected
_background: set[asyncio.Task] = set()


def _spawn(coro, *, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


async def delete_after(bot: Bot, chat_id: int, message_id: int, delay: float) -> None:
    """Sleep ``delay`` seconds, then safely delete ``message_id``.

    Fire-and-forget: never raises into the caller. Used for auto-cleaning
    public warnings so the group chat stays uncluttered.
    """
    await asyncio.sleep(delay)
    await safe_delete(bot, chat_id, message_id)


async def safe_delete(bot: Bot, chat_id: int, message_id: int) -> bool:
    """Delete one message. Never raises.

    The whole violation pipeline calls this *before* the penalty (mute,
    permanent mute, strike), so an unexpected Telegram error here must never
    be able to skip the sanction - ``False`` simply reports "not deleted".
    """
    try:
        await bot.delete_message(chat_id, message_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        return False
    except Exception as exc:
        log.warning("delete failed chat=%s msg=%s: %s", chat_id, message_id, exc)
        return False
    # every deletion our stack performs must be attributable in the logs
    log.info("DELETE chat=%s msg=%s", chat_id, message_id)
    return True


async def mute(bot: Bot, chat_id: int, user_id: int, seconds: int) -> bool:
    try:
        await bot.restrict_chat_member(
            chat_id,
            user_id,
            permissions=NO_SEND,
            until_date=datetime.now(UTC) + timedelta(seconds=seconds),
        )
        return True
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.warning("mute failed chat=%s user=%s: %s", chat_id, user_id, exc)
        return False


async def ban(bot: Bot, chat_id: int, user_id: int, revoke: bool = False) -> bool:
    """Manual removal - **admin-issued only** (`/بن` in modcmd).

    The autonomous ladder never reaches this function: `handle_ban_event()`
    turns every ban-worthy infraction into `perm_mute()` instead.
    """
    try:
        await bot.ban_chat_member(chat_id, user_id, revoke_messages=revoke)
        return True
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.warning("ban failed chat=%s user=%s: %s", chat_id, user_id, exc)
        return False


async def perm_mute(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Permanent mute: the offender keeps the membership but loses every send right.

    This is the **only** autonomous sanction the bot applies to a person: a
    ban-worthy infraction (strike ceiling or critical violation) lands here
    instead of ``ban_chat_member``, so the offender stays readable and auditable
    inside the group while the assigned Level 2 owner makes the final call.
    No ``until_date`` is passed, so the restriction never expires on its own.
    """
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=ChatPermissions(can_send_messages=False),
        )
        return True
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.warning("permanent mute failed chat=%s user=%s: %s", chat_id, user_id, exc)
        return False


async def set_slow_mode(bot: Bot, chat_id: int, seconds: int) -> bool:
    """
    NOTE: the Bot API exposes no `setChatSlowMode` method - slow mode can only
    be toggled from a Telegram client (MTProto). This adapter therefore uses
    the method only when a custom MTProto-backed bot provides it and returns
    False otherwise, letting the caller fall back to a temporary group mute.
    """
    method = getattr(bot, "set_chat_slow_mode", None)
    if method is None:
        return False
    try:
        await method(chat_id, slow_mode_delay=seconds)
        return True
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.warning("slow mode failed chat=%s: %s", chat_id, exc)
        return False


async def restore_chat(bot: Bot, chat_id: int, old_permissions: ChatPermissions) -> None:
    try:
        await bot.set_chat_permissions(chat_id, permissions=old_permissions)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.warning("restore permissions failed chat=%s: %s", chat_id, exc)


async def temp_mute_chat(bot: Bot, chat_id: int, seconds: int) -> None:
    """
    Join-flood response: freeze the whole group for a short while WITHOUT
    banning anybody, then hand the original permissions back.
    """
    if await rk.get_redis().exists(rk.k_join_lock(chat_id)):
        return
    await rk.get_redis().set(rk.k_join_lock(chat_id), "1", ex=seconds)

    try:
        chat = await bot.get_chat(chat_id)
        old = chat.permissions or ChatPermissions(can_send_messages=True)
    except Exception:
        old = ChatPermissions(can_send_messages=True)

    try:
        await bot.set_chat_permissions(chat_id, permissions=ChatPermissions(can_send_messages=False))
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.warning("join-lock failed chat=%s: %s", chat_id, exc)
        await rk.get_redis().delete(rk.k_join_lock(chat_id))
        return

    # best effort: real slow mode when an MTProto-capable bot is available,
    # otherwise the group-wide mute above already provides "slow mode like"
    # behaviour for the duration of the lock.
    await set_slow_mode(bot, chat_id, settings.join_slow_mode)

    async def _release() -> None:
        await asyncio.sleep(seconds)
        await restore_chat(bot, chat_id, old)
        await rk.get_redis().delete(rk.k_join_lock(chat_id))
        log.info("join-lock released chat=%s", chat_id)

    _spawn(_release(), name=f"joinlock:{chat_id}")


# --------------------------------------------------------------------------
# Master <-> Worker event bus
# --------------------------------------------------------------------------
async def publish_ban_event(
    *,
    chat_id: int,
    user_id: int,
    reason: str,
    message_id: int | None = None,
    score: float | None = None,
    actor_id: int | None = None,
    actor_source: str | None = None,
    target_name: str | None = None,
    target_username: str | None = None,
    content_report: bool = False,
    message_text: str | None = None,
    strike_count: int | None = None,
    action_taken: str | None = None,
) -> None:
    payload = json.dumps(
        {
            "chat_id": chat_id,
            "user_id": user_id,
            "reason": reason,
            "message_id": message_id,
            "score": score,
            # who executed it: admin id (human), or which bot did it on its own
            "actor_id": actor_id,
            "actor_source": actor_source,
            # how the offender signs in the private mod-log card
            "target_name": target_name,
            "target_username": target_username,
            # content violations use the report card (username + numeric id)
            "content_report": content_report,
            # verbatim user message for the ModLog card
            "message_text": message_text,
            # escalating strike info
            "strike_count": strike_count,
            "action_taken": action_taken,
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    )
    r = rk.get_redis()
    pipe = r.pipeline(transaction=False)
    # durable queue - the master consumes it with BRPOP
    pipe.rpush(rk.BAN_QUEUE, payload)
    # explicit flag so an operator can audit "why was this message removed"
    if message_id is not None:
        pipe.set(rk.k_porn_flag(chat_id, message_id), reason, ex=7 * 24 * 3600)
    pipe.expire(rk.BAN_QUEUE, 7 * 24 * 3600)
    await pipe.execute()


async def handle_ban_event(bot: Bot, raw: str, me_id: int) -> None:
    try:
        data = json.loads(raw)
        chat_id = int(data["chat_id"])
        user_id = int(data["user_id"])
        reason = str(data.get("reason", "policy"))
        raw_mid = data.get("message_id")
        try:
            message_id = int(raw_mid) if raw_mid is not None else None
        except (TypeError, ValueError):
            message_id = None
        score = data.get("score")
        raw_actor = data.get("actor_id")
        actor_id = int(raw_actor) if raw_actor is not None else None
        actor_source = data.get("actor_source")
        actor_source = str(actor_source) if actor_source else None
        raw_name = data.get("target_name")
        target_name = str(raw_name) if raw_name else None
        raw_username = data.get("target_username")
        target_username = str(raw_username).lstrip("@") if raw_username else None
        content_report = bool(data.get("content_report"))
        raw_message_text = data.get("message_text")
        message_text = str(raw_message_text) if raw_message_text else None
        raw_strike_count = data.get("strike_count")
        strike_count = int(raw_strike_count) if raw_strike_count is not None else None
        raw_action_taken = data.get("action_taken")
        action_taken = str(raw_action_taken) if raw_action_taken else None
    except (ValueError, KeyError, TypeError):
        log.error("malformed ban event: %s", raw[:200])
        return

    if user_id == me_id:
        return

    # The offending message is deleted right here, next to the sanction, so a
    # content violation (porn media, severe bad word) can never end up muted
    # but still on screen. The producer - `delete_and_ban()` used by the
    # watchdog and by the guard ladder - already deletes first, which makes
    # this a no-op in the normal case; it is the safety net for a delete that
    # failed there (lost rights, rate limit). `safe_delete()` never raises, so
    # the penalty below always runs.
    if message_id:
        await safe_delete(bot, chat_id, message_id)

    # `delete_and_mute()` shares the same durable queue as a real ban so that
    # temporary sanctions survive a restart too. The reason carries the intent:
    # "permamute:<reason>" -> permanent restriction, "mute:<reason>:<seconds>s"
    # -> timed restrict, anything else -> a ban-worthy infraction.
    #
    # INVARIANT: this function NEVER calls `ban_chat_member`. Every autonomous
    # sanction on a person is a permanent mute; a ban-worthy infraction is
    # additionally escalated to the assigned Level 2 (Owner) in private, and
    # only that human may reply «بن» to actually remove the member.
    perma_hit = PERMA_REASON.match(reason)
    mute_hit = MUTE_REASON.match(reason)
    escalate_owner = False
    if perma_hit is not None:
        await perm_mute(bot, chat_id, user_id)
        action = "PERMUTE"
        label = "سکوت دائم (Permanent Mute)"
        reason = perma_hit.group("reason")
        content_report = True
        escalate_owner = True
    elif mute_hit is not None:
        seconds = int(mute_hit.group("seconds"))
        await mute(bot, chat_id, user_id, seconds)
        action = f"MUTE {seconds}s"
        label = f"سکوت {seconds} ثانیه"
    else:
        await perm_mute(bot, chat_id, user_id)
        action = "PERMUTE"
        label = "سکوت دائم (Permanent Mute)"
        escalate_owner = True

    try:
        await BanLog.create(
            chat_id=chat_id,
            user_id=user_id,
            reason=reason,
            message_id=message_id,
            score=score,
        )
    except Exception:
        log.exception("failed to persist ban log")
    log.info("%s user=%s chat=%s reason=%s", action, user_id, chat_id, reason)

    # private mod-log: the assigned admins get the record in their DMs.
    # Fire-and-forget by construction - never delays the sanction itself.
    # `bot` is always the Master bot here (the bus hands us its handle), so the
    # card travels straight to the admins' PVs - never back into the group.
    # Content violations switch to the dedicated report card, which also
    # carries the offender's numeric id.
    extra: dict[str, str | None] = {}
    if content_report:
        extra["template"] = "content"
        extra["target_name"] = target_name
        if target_username:
            extra["target_username"] = target_username
    modlog.notify(
        bot,
        chat_id,
        action=label,
        target=user_id,
        actor=actor_id,
        actor_source=actor_source,
        reason=reason,
        message_text=message_text,
        strike_count=strike_count,
        action_taken=action_taken,
        **extra,
    )

    # level-2 escalation: a permanent mute is a *pending decision*, not a
    # verdict. Only the assigned owner of this chat gets the private alert,
    # and only they may reply «بن» to remove the member.
    if escalate_owner:
        escalation: dict[str, str | int | None] = {
            "target": user_id,
            "reason": reason,
            "target_name": target_name,
        }
        if target_username:
            escalation["target_username"] = target_username
        modlog.escalate(bot, chat_id, **escalation)


async def delete_and_ban(
    bot: Bot,
    *,
    chat_id: int,
    user_id: int,
    message_id: int | None,
    reason: str,
    score: float | None = None,
    notify: str | None = None,
    actor_id: int | None = None,
    actor_source: str | None = None,
    target_name: str | None = None,
    target_username: str | None = None,
    content_report: bool = False,
) -> None:
    """Delete the offending message and queue the sanction event.

    Kept under its historical name for the event-bus contract, but it no longer
    bans: `handle_ban_event()` resolves a ban-worthy reason into a permanent
    mute plus a private Level 2 escalation. Pass ``reason="permamute:..."`` to
    make the intent explicit, or a plain reason for the strike-ceiling path.

    If the message cannot be deleted (e.g., admin/creator immunity), the
    sanction is still applied but the public notice is suppressed to avoid
    falsely claiming the message was removed.
    """
    deleted = True
    if message_id:
        deleted = await safe_delete(bot, chat_id, message_id)
        if not deleted:
            log.warning(
                "delete_and_ban: could not delete msg_id=%s chat=%s user=%s "
                "(likely admin/creator immunity)",
                message_id, chat_id, user_id,
            )
    await publish_ban_event(
        chat_id=chat_id,
        user_id=user_id,
        reason=reason,
        message_id=message_id,
        score=score,
        actor_id=actor_id,
        actor_source=actor_source,
        target_name=target_name,
        target_username=target_username,
        content_report=content_report,
    )
    # The Master bot's dedicated Redis subscriber applies the sanction -
    # both the Worker and the Master funnel through the very same event bus.
    if notify and deleted:
        try:
            await bot.send_message(chat_id, redact_public_reason(notify))
        except Exception:
            pass


async def delete_and_mute(
    bot: Bot,
    *,
    chat_id: int,
    user_id: int,
    message_id: int | None,
    seconds: int,
    reason: str,
    actor_id: int | None = None,
    actor_source: str | None = None,
    target_name: str | None = None,
    target_username: str | None = None,
    content_report: bool = False,
) -> None:
    deleted = True
    if message_id:
        deleted = await safe_delete(bot, chat_id, message_id)
        if not deleted:
            log.warning(
                "delete_and_mute: could not delete msg_id=%s chat=%s user=%s "
                "(likely admin/creator immunity)",
                message_id, chat_id, user_id,
            )
    await mute(bot, chat_id, user_id, seconds)
    await publish_ban_event(
        chat_id=chat_id,
        user_id=user_id,
        reason=f"mute:{reason}:{seconds}s",
        actor_id=actor_id,
        actor_source=actor_source,
        target_name=target_name,
        target_username=target_username,
        content_report=content_report,
    )
    log.info("MUTED user=%s chat=%s for %ss (%s)", user_id, chat_id, seconds, reason)
