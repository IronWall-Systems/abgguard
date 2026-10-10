from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.types import Message

from abgguard.core import rbac, settings_store
from abgguard.core import redis as rk
from abgguard.core.rbac import Role
from abgguard.services import antiflood, strikes, textguards
from abgguard.services.harvester import harvester
from abgguard.services.moderation import delete_and_ban, delete_and_mute, safe_delete
from abgguard.services.textguards import find_emoji, find_link, is_bot_message

log = logging.getLogger("abgguard.master.guards")

router = Router(name="master.guards")
router.message.filter(
    F.chat.type.in_({"group", "supergroup"}),
    F.text,
    F.text.func(lambda t: not t.startswith("/")),
)

MSG_CACHE_TTL = 7 * 24 * 3600


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
    first_mute: int = 600,
    target_name: str | None = None,
) -> None:
    """1st strike -> timed mute, repeat strike -> permanent mute + Level 2 alert.

    The bot never bans: hitting the ceiling silences the member forever and
    hands the final «بن» decision to the assigned group owner in private.
    """
    await safe_delete(bot, chat_id, message_id)
    await _mark_dirty(chat_id, message_id)
    strike = await strikes.register(chat_id, user_id)

    if strike <= 1:
        await delete_and_mute(
            bot,
            chat_id=chat_id,
            user_id=user_id,
            message_id=None,
            seconds=first_mute,
            reason=reason,
            actor_source="master",
        )
        try:
            await bot.send_message(
                chat_id,
                f"⚠️ اخطار ۱ برای {_mention_id(user_id)}\n"
                f"دلیل: {reason}\n"
                f"تا {first_mute // 60} دقیقه سکوت.",
            )
        except Exception:
            pass
        return

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
                first_mute=600,
                target_name=user.full_name,
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
            reason=f"کلمه رکیک ({hit})",
            first_mute=600,
            target_name=user.full_name,
        )
        return

    # -------- 6. CLEAN -> dataset -----------------------------------------
    await _harvest(bot, message)
