from __future__ import annotations

import asyncio
import html
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from abgguard.config import settings
from abgguard.core import rbac, settings_store
from abgguard.core import redis as rk
from abgguard.core.rbac import Role
from abgguard.services import antiflood
from abgguard.services.moderation import NO_SEND, temp_mute_chat

log = logging.getLogger("abgguard.master.members")

router = Router(name="master.members")

_JOINING = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.RESTRICTED,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.CREATOR,
}
_LEFT = {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}

# what a verified member gets back (Rose-style default member permissions)
FULL_SEND = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_change_info=False,
    can_invite_users=True,
    can_pin_messages=False,
)


def _is_join(event: ChatMemberUpdated) -> bool:
    return (
        event.old_chat_member.status in _LEFT
        and event.new_chat_member.status in _JOINING
    )


def _is_leave(event: ChatMemberUpdated) -> bool:
    return (
        event.old_chat_member.status in _JOINING
        and event.new_chat_member.status in _LEFT
    )


async def _handle_join(bot: Bot, chat_id: int, user_id: int, token: str) -> bool:
    """
    JOIN FLOOD (advertising waves): >100 joins / 60s
    -> freeze the group (slow-mode-like mute) but NEVER ban anybody.
    """
    kind = await antiflood.check_join_flood(chat_id, user_id, token)
    if kind is not antiflood.FloodKind.JOIN:
        return False

    already_locked = await antiflood.is_join_locked(chat_id)
    if not already_locked:
        await temp_mute_chat(bot, chat_id, settings.join_lock_seconds)
        log.warning(
            "JOIN FLOOD chat=%s -> group locked for %ss (no bans)",
            chat_id, settings.join_lock_seconds,
        )
        try:
            await bot.send_message(
                chat_id,
                "🚨 موج عضویت شناسایی شد!\n"
                "گروه موقتاً برای جلوگیری از تبلیغات قفل شد "
                f"({settings.join_lock_seconds} ثانیه).\n"
                "هیچ کاربری بن نمی‌شود.",
            )
        except Exception:
            pass
    return True


async def _cleanup_join_messages(bot: Bot, chat_id: int, message_ids: list[int]) -> None:
    if not await antiflood.is_join_locked(chat_id):
        return
    for mid in message_ids:
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass


# ------------------------------------------------------- chat_member events --
@router.chat_member()
async def on_chat_member_updated(event: ChatMemberUpdated, bot: Bot) -> None:
    chat_id = event.chat.id
    if event.chat.type not in {"group", "supergroup"}:
        return

    if _is_join(event):
        user = event.new_chat_member.user
        token = f"{chat_id}:{user.id}:{event.date.timestamp()}"
        locked = await _handle_join(bot, chat_id, user.id, token)
        if locked:
            if event.invite_link is None:
                # advertising bots often join via direct deep links
                log.debug("join flood participant=%s chat=%s", user.id, chat_id)
        elif not user.is_bot:
            await _greet(bot, chat_id, user)
        return

    if _is_leave(event):
        await rk.get_redis().delete(
            f"{rk.RBAC_CACHE}:{chat_id}:{event.old_chat_member.user.id}"
        )


# --------------------------------------------------- new_chat_members msgs ---
@router.message(F.chat.type.in_({"group", "supergroup"}), F.new_chat_members)
async def on_new_members(message: Message, bot: Bot) -> None:
    chat_id = message.chat.id
    users = message.new_chat_members or []

    for user in users:
        if user.is_bot:
            continue
        token = f"{chat_id}:{user.id}:{message.message_id}"
        locked = await _handle_join(bot, chat_id, user.id, token)
        if not locked:
            await _greet(bot, chat_id, user)

    await _cleanup_join_messages(bot, chat_id, [message.message_id])


@router.message(F.chat.type.in_({"group", "supergroup"}), F.left_chat_member)
async def on_left_member(message: Message) -> None:
    if not await antiflood.is_join_locked(message.chat.id):
        return
    try:
        await message.delete()
    except Exception:
        pass


# ================================================ welcome + anti-bot CAPTCHA ==
_background: set[asyncio.Task] = set()


def _spawn(coro, *, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


def _k_captcha(chat_id: int, user_id: int) -> str:
    return f"{rk.P}:captcha:{chat_id}:{user_id}"


class _SafeMap(dict):
    """{unknown} placeholders are left untouched instead of raising."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _render_welcome(template: str, user, chat_id: int) -> str:
    name = html.escape(user.full_name or str(user.id))
    mapping = {
        "mention": f'<a href="tg://user?id={user.id}">{name}</a>',
        "name": name,
        "id": str(user.id),
        "chat": str(chat_id),
    }
    try:
        return template.format_map(_SafeMap(mapping))
    except (IndexError, KeyError, ValueError):
        return template


def _verify_kb(chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ عضویت را تایید می‌کنم | I'm human",
                    callback_data=f"cap:ok:{chat_id}:{user_id}",
                )
            ]
        ]
    )


async def _greet(bot: Bot, chat_id: int, user) -> None:
    """Count the member, run the CAPTCHA when enabled, then welcome them."""
    r = rk.get_redis()
    pipe = r.pipeline(transaction=False)
    pipe.pfadd(rk.STATS_MEMBERS, f"{chat_id}:{user.id}")
    pipe.pfadd(rk.k_stats_seen(chat_id), str(user.id))
    await pipe.execute()

    policy = await settings_store.get_policy(chat_id)
    needs_captcha = policy.captcha_enabled and (
        await rbac.get_role(chat_id, user.id) < Role.SUBADMIN
    )
    if needs_captcha and await _start_captcha(bot, chat_id, user):
        return  # the welcome is delivered once the CAPTCHA is answered

    if policy.welcome_text:
        await _send_welcome(bot, chat_id, user, policy.welcome_text)


async def _send_welcome(bot: Bot, chat_id: int, user, template: str) -> None:
    try:
        await bot.send_message(chat_id, _render_welcome(template, user, chat_id))
    except Exception:
        log.debug("welcome failed chat=%s", chat_id, exc_info=True)


async def _start_captcha(bot: Bot, chat_id: int, user) -> bool:
    """
    Mute the newcomer and ask for one inline confirmation.
    Returns False when the restriction could not be applied (bot without admin
    rights) - the caller then falls back to a plain welcome.
    """
    r = rk.get_redis()
    key = _k_captcha(chat_id, user.id)
    if await r.exists(key):
        return True

    try:
        await bot.restrict_chat_member(chat_id, user.id, permissions=NO_SEND)
    except Exception:
        log.debug("captcha restrict failed chat=%s user=%s", chat_id, user.id,
                  exc_info=True)
        return False

    name = html.escape(user.full_name or str(user.id))
    try:
        prompt = await bot.send_message(
            chat_id,
            f"🛡 تایید عضویت\n"
            f"به گروه خوش آمدی {name}!\n"
            f"برای ارسال پیام، حداکثر تا {settings.captcha_timeout} ثانیه "
            "دکمه زیر را بزنید.",
            reply_markup=_verify_kb(chat_id, user.id),
        )
    except Exception:
        log.debug("captcha prompt failed chat=%s", chat_id, exc_info=True)
        return False

    await r.set(key, str(prompt.message_id), ex=settings.captcha_timeout)
    _spawn(_captcha_expiry(bot, chat_id, user.id, prompt.message_id),
           name=f"captcha:{chat_id}:{user.id}")
    return True


async def _captcha_expiry(bot: Bot, chat_id: int, user_id: int, prompt_id: int) -> None:
    """No verification in time -> kick the account (never a mass ban)."""
    await asyncio.sleep(settings.captcha_timeout)
    r = rk.get_redis()
    if not await r.delete(_k_captcha(chat_id, user_id)):
        return  # already verified
    try:
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id)
    except Exception:
        log.debug("captcha kick failed chat=%s user=%s", chat_id, user_id,
                  exc_info=True)
    try:
        await bot.delete_message(chat_id, prompt_id)
    except Exception:
        pass


@router.callback_query(F.data.startswith("cap:"))
async def on_captcha_click(cq: CallbackQuery, bot: Bot) -> None:
    parts = (cq.data or "").split(":")
    if len(parts) < 4:
        await cq.answer()
        return
    try:
        chat_id, user_id = int(parts[2]), int(parts[3])
    except ValueError:
        await cq.answer()
        return

    actor = cq.from_user
    if actor is None or actor.id != user_id:
        await cq.answer("این دکمه برای کاربر دیگری است.", show_alert=True)
        return
    msg = cq.message
    if isinstance(msg, Message) and msg.chat.id != chat_id:
        # a copied prompt must not lift a restriction in another chat
        await cq.answer("این دکمه معتبر نیست.", show_alert=True)
        return

    r = rk.get_redis()
    if not await r.delete(_k_captcha(chat_id, user_id)):
        await cq.answer("قبلاً تایید شده است.", show_alert=True)
        return

    try:
        await bot.restrict_chat_member(chat_id, user_id, permissions=FULL_SEND)
    except Exception:
        log.debug("captcha lift failed chat=%s", chat_id, exc_info=True)

    msg = cq.message
    if isinstance(msg, Message):
        try:
            await msg.delete()
        except Exception:
            pass

    await cq.answer("✅ عضویت تایید شد.")

    policy = await settings_store.get_policy(chat_id)
    if policy.welcome_text:
        await _send_welcome(bot, chat_id, actor, policy.welcome_text)
