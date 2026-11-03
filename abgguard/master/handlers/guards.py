from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.types import Message

from abgguard.core import rbac, settings_store
from abgguard.core import redis as rk
from abgguard.core.rbac import Role
from abgguard.services import antiflood, strikes, subscription, textguards
from abgguard.services.harvester import harvester
from abgguard.services.moderation import (
    _spawn,
    delete_after,
    delete_and_ban,
    delete_and_mute,
    mute,
    publish_ban_event,
    safe_delete,
)
from abgguard.services.textguards import find_emoji, find_link, is_bot_message

log = logging.getLogger("abgguard.master.guards")

router = Router(name="master.guards")
router.message.filter(
    F.chat.type.in_({"group", "supergroup"}),
    F.text,
    F.text.func(lambda t: not t.startswith("/")),
)

MSG_CACHE_TTL = 7 * 24 * 3600

# Escalating mute durations (seconds) keyed by strike count.
# Strikes 1-4: no mute (delete only). Strike 5+: progressive mute.
_STRIKE_MUTE_SECONDS: dict[int, int] = {
    5: 300,      # 5 minutes
    6: 600,      # 10 minutes
    7: 1800,     # 30 minutes
    8: 3600,     # 1 hour
}
_STRIKE_MUTE_DEFAULT = 86400  # 24 hours for strike 9+

_STRIKE_DURATION_TEXT: dict[int, str] = {
    5: "۵ دقیقه",
    6: "۱۰ دقیقه",
    7: "۳۰ دقیقه",
    8: "۱ ساعت",
}
_STRIKE_DURATION_DEFAULT = "۲۴ ساعت"


def _k_msg(chat_id: int, message_id: int) -> str:
    return f"{rk.P}:msg:{chat_id}:{message_id}"


def _k_dirty(chat_id: int, message_id: int) -> str:
    return f"{rk.P}:dirty:{chat_id}:{message_id}"


def _mention_id(user_id: int) -> str:
    return f'<a href="tg://user?id={user_id}">{user_id}</a>'


# ------------------------------------------------------------- msg context --
async def _cache_message(chat_id: int, message_id: int, user_id: int, text: str) -> None:
    """Cache the text (harvest context) and count the author in one round trip."""
    r = rk.get_redis()
    pipe = r.pipeline(transaction=False)
    pipe.set(_k_msg(chat_id, message_id), text, ex=MSG_CACHE_TTL)
    pipe.pfadd(rk.STATS_MEMBERS, f"{chat_id}:{user_id}")
    pipe.pfadd(rk.k_stats_seen(chat_id), str(user_id))
    await pipe.execute()


async def _mark_dirty(chat_id: int, message_id: int) -> None:
    """Flag a message we deleted for policy - it must never be harvested."""
    await rk.get_redis().set(_k_dirty(chat_id, message_id), "1", ex=MSG_CACHE_TTL)


# ---------------------------------------------------------------- penalties --
async def _escalate(
    bot: Bot,
    *,
    chat_id: int,
    user_id: int,
    message_id: int,
    reason: str,
    log_reason: str | None = None,
    target_name: str | None = None,
    target_username: str | None = None,
    content_report: bool = False,
    message_text: str | None = None,
) -> None:
    """Escalating strike system for profanity & NSFW violations.

    Always deletes the offending message, then increments a Redis strike
    counter (TTL 24h). Mutes only when strike >= 5 with progressive duration.
    Sends an auto-deleting public warning and a PV ModLog card.
    """
    private_reason = log_reason or reason
    await safe_delete(bot, chat_id, message_id)
    await _mark_dirty(chat_id, message_id)

    # Increment strike counter (24h TTL).
    strike_key = rk.k_strike(chat_id, user_id)
    r = rk.get_redis()
    strike_count = await r.incr(strike_key)
    if strike_count == 1:
        await r.expire(strike_key, 86400)

    # Determine mute duration based on strike count.
    mute_seconds = _STRIKE_MUTE_SECONDS.get(strike_count, _STRIKE_MUTE_DEFAULT)
    should_mute = strike_count >= 5

    # Build public warning text.
    mention = _mention_id(user_id)
    if strike_count < 5:
        warning_text = (
            f"⚠️ کاربر {mention}، پیام شما به دلیل محتوای نامناسب پاک شد. "
            f"(اخطار {strike_count}/4)"
        )
    else:
        duration_text = _STRIKE_DURATION_TEXT.get(strike_count, _STRIKE_DURATION_DEFAULT)
        warning_text = (
            f"🔇 کاربر {mention} به دلیل تکرار تخلف ({strike_count} بار) "
            f"به مدت {duration_text} سکوت شد."
        )

    # Apply mute if strike >= 5.
    if should_mute:
        await mute(bot, chat_id, user_id, mute_seconds)

    # Send auto-deleting public warning.
    try:
        warning = await bot.send_message(chat_id, warning_text)
        _spawn(delete_after(bot, chat_id, warning.message_id, 30.0), name="warn-clean")
    except Exception:
        pass

    # Queue the sanction event for the ModLog PV card.
    action_taken = (
        "حذف پیام فقط"
        if not should_mute
        else f"سکوت {mute_seconds // 60} دقیقه"
        if mute_seconds < 86400
        else "سکوت ۲۴ ساعته"
    )
    await publish_ban_event(
        chat_id=chat_id,
        user_id=user_id,
        reason=private_reason,
        message_id=message_id,
        actor_source="master",
        target_name=target_name,
        target_username=target_username,
        content_report=content_report,
        message_text=message_text,
        strike_count=strike_count,
        action_taken=action_taken,
    )


async def _strike_only(
    bot: Bot,
    *,
    chat_id: int,
    user_id: int,
    message_id: int,
    reason: str,
    target_name: str | None = None,
) -> None:
    """Content locks: delete + count. The 3rd strike turns into a permanent mute."""
    await safe_delete(bot, chat_id, message_id)
    await _mark_dirty(chat_id, message_id)
    strike = await strikes.register(chat_id, user_id)
    if strike > 2:
        await delete_and_ban(
            bot,
            chat_id=chat_id,
            user_id=user_id,
            message_id=None,
            reason=reason,
            notify=f"🔇 {_mention_id(user_id)} به دلیل تکرار تخلف ({reason}) "
            "برای همیشه سکوت شد؛ تصمیم نهایی با مالک گروه است.",
            actor_source="master",
            target_name=target_name,
        )


async def _delete_only(bot: Bot, chat_id: int, message_id: int) -> None:
    await safe_delete(bot, chat_id, message_id)
    await _mark_dirty(chat_id, message_id)


# ----------------------------------------------------------------- harvest --
async def _harvest(bot: Bot, message: Message) -> None:
    """Silently log {context, reply} pairs - text messages only."""
    if message.reply_to_message is None or not message.text:
        return

    chat_id = message.chat.id
    origin_id = message.reply_to_message.message_id
    r = rk.get_redis()

    if await r.exists(_k_dirty(chat_id, origin_id)):
        return  # the context message violated a rule - keep it out of the dataset

    context = await r.get(_k_msg(chat_id, origin_id))
    if not context:
        log.debug("harvest miss chat=%s msg=%s (older than cache)", chat_id, origin_id)
        return

    try:
        await harvester.record(
            chat_id=chat_id,
            context=context,
            reply=message.text,
            user_id=message.from_user.id if message.from_user else None,
            reply_to_id=origin_id,
        )
    except Exception:
        log.exception("harvest record failed")


# ------------------------------------------------------------------- main ---
@router.message()
async def guard_text(message: Message, bot: Bot) -> None:
    text = message.text
    user = message.from_user
    if is_bot_message(message) or user is None or not text:
        # bots never moderate bots - the glass panel / notices we send ourselves
        # must never re-enter the locks (emoji, link, bad words, flood).
        return

    chat_id = message.chat.id

    # SaaS gate: UNPAID / EXPIRED chats never reach the guards (CPU saving).
    if not await subscription.is_active(chat_id):
        return

    role = await rbac.get_role(chat_id, user.id)

    # every message becomes a potential "context" for a later reply
    await _cache_message(chat_id, message.message_id, user.id, text)

    # -------- 1. SMART TEXT FLOOD (rate limit + repetition) ---------------
    if role < Role.SUBADMIN:
        kind = await antiflood.check_text_flood(chat_id, user.id, text, message.message_id)
        if kind is not antiflood.FloodKind.NONE:
            await _escalate(
                bot,
                chat_id=chat_id,
                user_id=user.id,
                message_id=message.message_id,
                reason=kind.fa,
                target_name=user.full_name,
                target_username=user.username,
                message_text=text,
            )
            return

    if role >= Role.SUBADMIN:
        # trusted staff: never filtered
        await _harvest(bot, message)
        return

    flags = await settings_store.get_flags(chat_id)

    # -------- 2. FORWARD LOCK --------------------------------------------
    if flags.forward_lock and textguards.is_forwarded(message):
        await _strike_only(
            bot,
            chat_id=chat_id,
            user_id=user.id,
            message_id=message.message_id,
            reason="قفل فوروارد",
            target_name=user.full_name,
        )
        return

    # -------- 3. LINK LOCK ------------------------------------------------
    if flags.link_lock:
        found = find_link(text)
        if found:
            await delete_and_mute(
                bot,
                chat_id=chat_id,
                user_id=user.id,
                message_id=message.message_id,
                seconds=300,
                reason="قفل لینک",
                actor_source="master",
            )
            await _mark_dirty(chat_id, message.message_id)
            try:
                await bot.send_message(chat_id, "🔗 ارسال لینک در این گروه ممنوع است.")
            except Exception:
                pass
            return

    # -------- 4. EMOJI LOCK ----------------------------------------------
    if flags.emoji_lock and find_emoji(text):
        await _strike_only(
            bot,
            chat_id=chat_id,
            user_id=user.id,
            message_id=message.message_id,
            reason="قفل ایموجی",
            target_name=user.full_name,
        )
        return

    # -------- 5. BAD WORDS ------------------------------------------------
    hit = await settings_store.contains_badword(chat_id, text)
    if hit:
        await _escalate(
            bot,
            chat_id=chat_id,
            user_id=user.id,
            message_id=message.message_id,
            reason="کلمه رکیک",
            log_reason=f"کلمه رکیک ({hit})",
            target_name=user.full_name,
            target_username=user.username,
            content_report=True,
            message_text=text,
        )
        return

    # -------- 6. CLEAN -> dataset -----------------------------------------
    await _harvest(bot, message)
