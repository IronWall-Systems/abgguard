from __future__ import annotations

import asyncio
import html
import logging
import re
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Filter
from aiogram.types import ChatPermissions, Message

from abgguard.core import hierarchy, rbac
from abgguard.core.rbac import Role
from abgguard.core.redis import (
    VIP_TTL,
    get_redis,
    k_title_protected,
    revoke_user_vip,
    set_user_vip,
)
from abgguard.master.ephemeral import delete_later, fa_num, notice, spawn
from abgguard.master.keyboards import wipe_confirm_kb
from abgguard.models.entities import BanLog
from abgguard.services import moderation, modlog, strikes, subscription
from abgguard.services.textguards import is_bot_message

log = logging.getLogger("abgguard.master.modcmd")

router = Router(name="master.modcmd")

GROUPS = {"group", "supergroup"}


async def _sub_active(chat_id: int) -> bool:
    """SaaS gate shared by every moderation command path (fail-closed)."""
    return await subscription.is_active(chat_id)


async def _require_subscription(message: Message, bot: Bot) -> bool:
    """Refuse the command in UNPAID/EXPIRED groups and show the renewal card."""
    if await _sub_active(message.chat.id):
        return True
    await subscription.send_expiry_notice(bot, message.chat.id)
    return False


DEFAULT_MUTE = 1800  # bare «سکوت» -> 30 minutes
WARN_MUTE = 600  # first manual warning stays 10 minutes
MAX_MUTE = 366 * 24 * 3600
DEFAULT_PURGE = 20
MAX_PURGE = 100
REASON_LIMIT = 64  # ban_log.reason column

# /mute, /سکوت and friends all resolve to the same canonical action.
ALIASES: dict[str, str] = {
    "mute": "mute",
    "silence": "mute",
    "سکوت": "mute",
    # permanent restriction - no expiry, the member keeps read-only access
    "full_mute": "full_mute",
    "fullmute": "full_mute",
    "ban": "ban",
    "kick": "ban",
    "بن": "ban",
    "warn": "warn",
    "اخطار": "warn",
    "purge": "purge",
    "clean": "purge",
    "wipe": "wipe",
    "wipeall": "wipe_all",
    "پاکسازی": "wipe",
    # hand the default send rights back after a mute
    "unmute": "unmute",
    "لغو سکوت": "unmute",
    "حذف سکوت": "unmute",
}

# Bare (slash-less) phrases for the full-history wipe. They are matched only
# for staff, exactly like the other plain triggers.
FULL_WIPE_TRIGGERS: dict[str, str] = {
    "پاکسازی همه": "wipe_all",
    "wipe all": "wipe_all",
}

# Default send rights restored by /unmute. `can_send_media_messages` left the
# Bot API in 7.0 (aiogram dropped it too), so the media half of the spec maps
# onto the granular audio/document/photo/video/video-note/voice-note flags;
# polls are part of the same "may I send anything" set.
DEFAULT_SEND_PERMISSIONS = ChatPermissions(
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
)

# Hard RBAC hierarchy (rule §3) enforced in `on_mod_command`:
#   Level 2 (Owner) / Level 3 (God) -> may ban
#   Level 1 (Sub-Admin)             -> may only silence and warn
BAN_NOTICE = "⚠️ شما دسترسی بن کردن ندارید."

# Words that also work *without* a slash when written alone in the group.
# Only the two triggers named by project rule 6 - "بن" stays command-only
# because the bare word is far too common to consume silently.
PLAIN_TRIGGERS = frozenset({"سکوت", "اخطار", "پاکسازی", "wipe"})

# Unmute, too, is reply-driven and staff-only: these two spellings work as a
# bare message, "لغو سکوت" / "حذف سکوت", alongside the /unmute command.
UNMUTE_TRIGGERS = frozenset({"لغو سکوت", "حذف سکوت"})

# Reply-based VIP triggers: a staff reply containing one of these grants (or
# revokes) the temporary 3-hour media whitelist for the replied-to member.
VIP_GRANT_WORDS = ("ویژه", "وی‌آی‌پی")
VIP_REVOKE_WORDS = ("لغو ویژه", "لغو وی‌آی‌پی")

# Organizational rank -> operational RBAC level. Senior ranks can ban, so they
# map to Level 2; chat/call admins map to Level 1 (mute/warn only).
HIERARCHY_OPERATIONAL_LEVEL: dict[str, Role] = {
    hierarchy.Role.DEPUTY: Role.ADMIN,
    hierarchy.Role.SENIOR_CHAT: Role.ADMIN,
    hierarchy.Role.SENIOR_CALL: Role.ADMIN,
    hierarchy.Role.ADMIN_CHAT: Role.SUBADMIN,
    hierarchy.Role.ADMIN_CALL: Role.SUBADMIN,
    hierarchy.Role.ADMIN_CHAT_CALL: Role.SUBADMIN,
}

# Reply-driven silent cleanup: an admin answers a message with exactly one of
# these two words and both messages disappear - no warning, no mute and no
# confirmation notice (Clean Chat Protocol, §6). "حذف سکوت" is a different,
# already claimed trigger, so the bare word never collides with /unmute.
REMOVE_TRIGGERS = frozenset({"remove", "حذف"})
PIN_TRIGGERS = frozenset({"pin", "پین"})
UNPIN_TRIGGERS = frozenset({"unpin", "حذف پین"})

# ------------------------------------------------------------------ parsing --
def vip_action(text: str | None) -> str | None:
    """``"revoke"`` / ``"grant"`` for the VIP reply triggers, else ``None``."""
    raw = " ".join((text or "").split())
    if not raw:
        return None
    if any(word in raw for word in VIP_REVOKE_WORDS):
        return "revoke"
    if any(word in raw for word in VIP_GRANT_WORDS):
        return "grant"
    return None


def is_plain_trigger(text: str | None) -> bool:
    raw = (text or "").strip()
    if not raw or raw.startswith("/"):
        return False
    return (
        raw in PLAIN_TRIGGERS or raw in UNMUTE_TRIGGERS or raw in FULL_WIPE_TRIGGERS
    )


def is_remove_trigger(text: str | None) -> bool:
    """Exact match only: "remove" / "حذف", nothing longer and nothing looser."""
    raw = (text or "").strip()
    if not raw or raw.startswith("/"):
        return False
    return raw in REMOVE_TRIGGERS


def is_pin_trigger(text: str | None) -> bool:
    """step 6: exact "پین" / "pin" / "/pin" - reply decides what gets pinned."""
    raw = (text or "").strip()
    return raw in PIN_TRIGGERS or raw.split("@", 1)[0].lower() == "/pin"


def is_unpin_trigger(text: str | None) -> bool:
    """step 6: exact "حذف پین" / "unpin" / "/unpin"."""
    raw = (text or "").strip()
    return raw in UNPIN_TRIGGERS or raw.split("@", 1)[0].lower() == "/unpin"


def parse_mod_command(text: str | None) -> tuple[str, str] | None:
    """``("/mute", "30")`` for any accepted spelling, otherwise ``None``."""
    raw = (text or "").strip()
    if not raw:
        return None

    if raw.startswith("/"):
        parts = raw.split(maxsplit=1)
        head = parts[0][1:].split("@", 1)[0].lower()
        name = ALIASES.get(head)
        if name is None:
            return None
        return name, (parts[1].strip() if len(parts) > 1 else "")

    if raw in PLAIN_TRIGGERS or raw in UNMUTE_TRIGGERS:
        return ALIASES[raw], ""
    if raw in FULL_WIPE_TRIGGERS:
        return FULL_WIPE_TRIGGERS[raw], ""
    return None


def parse_mute_seconds(raw: str) -> int | None:
    """Strict mute parser: empty -> 30 min; ``X`` -> exactly X hours.

    Anything other than a positive integer is rejected so ``/mute 30`` can
    never silently mean 30 minutes when the product contract says hours.
    """
    token = (raw or "").strip().split(maxsplit=1)
    if not token:
        return DEFAULT_MUTE
    if not token[0].isdigit():
        return None
    hours = int(token[0])
    if hours <= 0:
        return None
    return min(hours * 3600, MAX_MUTE)


def parse_count(raw: str, default: int = DEFAULT_PURGE, maximum: int = MAX_PURGE) -> int:
    token = (raw or "").strip().split(maxsplit=1)
    if not token or not token[0].isdigit():
        return default
    return max(1, min(int(token[0]), maximum))


def resolve_target(reply_user_id: int | None, args: str) -> tuple[int | None, str]:
    """Resolve a moderation target **only** from the replied-to message.

    A raw number is deliberately not treated as a user id: ``/mute 30`` must
    mean "30 hours" (with a reply), never "user id 30".
    """
    return reply_user_id, (args or "").strip()


def humanize(seconds: int) -> str:
    if seconds < 60:
        return f"{fa_num(seconds)} ثانیه"
    if seconds < 3600:
        minutes, extra = divmod(seconds, 60)
        base = f"{fa_num(minutes)} دقیقه"
        return f"{base} و {fa_num(extra)} ثانیه" if extra else base
    if seconds < 86400:
        hours, extra = divmod(seconds, 3600)
        base = f"{fa_num(hours)} ساعت"
        return f"{base} و {fa_num(extra // 60)} دقیقه" if extra // 60 else base
    days, extra = divmod(seconds, 86400)
    base = f"{fa_num(days)} روز"
    return f"{base} و {fa_num(extra // 3600)} ساعت" if extra // 3600 else base


def _mention(user_id: int, name: str | None = None) -> str:
    label = html.escape(name or str(user_id))
    return f'<a href="tg://user?id={user_id}">{label}</a>'


# ------------------------------------------------------------------- filter --
class ModTrigger(Filter):
    """
    Matches only the moderation commands.

    A bare Persian trigger ("سکوت", "اخطار", "لغو سکوت", "حذف سکوت") is matched
    only for staff, so an ordinary member typing the same words keeps flowing
    through the normal text guards instead of being swallowed by this router.
    """

    async def __call__(self, message: Message) -> bool:
        parsed = parse_mod_command(message.text)
        if parsed is None:
            return False
        if not is_plain_trigger(message.text):
            return True
        user, chat = message.from_user, message.chat
        if user is None or chat is None or is_bot_message(message):
            return False
        return await rbac.get_role(chat.id, user.id) >= Role.SUBADMIN


class RoleCommandTrigger(Filter):
    """Matches only the hierarchical promotion/demotion commands.

    The old router-level catch-all swallowed every group text message before
    ``guards.router`` could see it, which silently disabled the profanity,
    link, emoji and forward filters.  This filter keeps the role commands in
    this router while letting ordinary text continue to the guards.
    """

    async def __call__(self, message: Message) -> bool:
        return (
            _parse_role_command(
                message.text,
                has_reply=message.reply_to_message is not None,
            )
            is not None
        )


AUDIT_ADMIN_TRIGGERS = frozenset({"بررسی ادمین", "audit_admins"})


def is_admin_audit_trigger(text: str | None) -> bool:
    """Match the Persian trigger or the slash command (with optional bot tag)."""
    raw = (text or "").strip()
    if not raw:
        return False
    if raw.startswith("/"):
        raw = raw[1:].strip()
    raw = " ".join(raw.split()).translate(_ARABIC_CHARS).lower()
    raw = raw.split("@", 1)[0].strip()
    return raw in AUDIT_ADMIN_TRIGGERS


class AdminAuditTrigger(Filter):
    """Matches «بررسی ادمین» / «/audit_admins» in groups."""

    async def __call__(self, message: Message) -> bool:
        return is_admin_audit_trigger(message.text)


class VipTrigger(Filter):
    """
    Matches a *reply* carrying a VIP trigger, written by staff only (Level 1-3).

    Requiring the reply keeps the plain word out of ordinary conversation: a
    member typing "ویژه" keeps flowing through the normal text guards.
    """

    async def __call__(self, message: Message) -> bool:
        if message.reply_to_message is None:
            return False
        if vip_action(message.text) is None:
            return False
        user, chat = message.from_user, message.chat
        if user is None or chat is None or is_bot_message(message):
            return False
        return await rbac.get_role(chat.id, user.id) >= Role.SUBADMIN


class RemoveTrigger(Filter):
    """
    Matches a *reply* reading exactly ``"remove"`` / ``"حذف"``, staff only.

    The reply is mandatory: the same word typed on its own stays ordinary
    conversation and keeps flowing through the normal text guards. A member
    (Level 0) never activates it either - the RBAC check below is the whole
    permission gate, so no refusal notice is ever sent for them.
    """

    async def __call__(self, message: Message) -> bool:
        if message.reply_to_message is None:
            return False
        if not is_remove_trigger(message.text):
            return False
        user, chat = message.from_user, message.chat
        if user is None or chat is None or is_bot_message(message):
            return False
        return await rbac.get_role(chat.id, user.id) >= Role.SUBADMIN


class PinTrigger(Filter):
    """step 6: a *reply* reading exactly ``"پین"`` / ``"pin"`` - staff only."""

    async def __call__(self, message: Message) -> bool:
        if message.reply_to_message is None:
            return False
        if not is_pin_trigger(message.text):
            return False
        user, chat = message.from_user, message.chat
        if user is None or chat is None or is_bot_message(message):
            return False
        return await rbac.get_role(chat.id, user.id) >= Role.SUBADMIN


class UnpinTrigger(Filter):
    """step 6: ``"حذف پین"`` / ``"unpin"`` anywhere in the group - staff only."""

    async def __call__(self, message: Message) -> bool:
        if not is_unpin_trigger(message.text):
            return False
        user, chat = message.from_user, message.chat
        if user is None or chat is None or is_bot_message(message):
            return False
        return await rbac.get_role(chat.id, user.id) >= Role.SUBADMIN


router.message.filter(F.chat.type.in_(GROUPS), F.text)


# ------------------------------------------------------------------- utils ---
async def _ack_delete(message: Message, bot) -> None:
    """Rule 6: the trigger itself disappears the moment it is seen."""
    await moderation.safe_delete(bot, message.chat.id, message.message_id)


async def _log_action(chat_id: int, user_id: int, reason: str) -> None:
    try:
        await BanLog.create(chat_id=chat_id, user_id=user_id, reason=reason[:REASON_LIMIT])
    except Exception:
        log.exception("failed to persist moderation log")


async def _check_target(bot, chat_id: int, actor: int, actor_role: Role, target: int) -> str | None:
    if target == actor:
        return "❌ نمی‌توانید این دستور را روی خودتان اجرا کنید."
    if target == (await bot.me()).id:
        return "❌ این دستور روی ربات اجرا نمی‌شود."
    target_role = await rbac.get_role(chat_id, target)
    if target_role >= actor_role:
        return "❌ این کاربر هم‌سطح یا بالاتر از شماست؛ اجازه اجرا ندارید."
    return None


async def _drop_replied(message: Message, bot) -> None:
    reply = message.reply_to_message
    if reply is not None:
        await moderation.safe_delete(bot, message.chat.id, reply.message_id)


def _actor(message: Message) -> int | None:
    user = message.from_user
    return user.id if user is not None else None


def _target_name(message: Message, target: int) -> str | None:
    """Display name of the replied-to member, when the command answered a reply."""
    reply = message.reply_to_message
    user = reply.from_user if reply is not None else None
    if user is not None and user.id == target:
        return user.full_name
    return None


# ------------------------------------------------------------------ actions --
async def _do_mute(message: Message, bot, target: int, args: str) -> None:
    """Dynamic mute: 'سکوت' alone -> 30 min; 'سکوت X' -> X hours."""
    chat_id = message.chat.id
    actor = message.from_user
    if actor is None:
        return

    # Moderation validation: only God, Deputy, Senior Chat, Chat Admin may mute
    actor_role = await hierarchy.get_role(chat_id, actor.id)
    if actor_role not in {
        hierarchy.Role.GOD,
        hierarchy.Role.DEPUTY,
        hierarchy.Role.SENIOR_CHAT,
        hierarchy.Role.ADMIN_CHAT,
        hierarchy.Role.ADMIN_CHAT_CALL,
    }:
        await notice(message, "❌ شما مجوز سکوت کردن ندارید.", delay=7.0)
        return

    # Parse duration: bare 'سکوت' -> 30 min; 'سکوت X' -> exactly X hours.
    seconds = parse_mute_seconds(args)
    if seconds is None:
        await notice(
            message,
            "❌ مدت سکوت باید عدد صحیح ساعت باشد. مثال: <code>سکوت 2</code>",
            delay=7.0,
        )
        return

    if not await moderation.mute(bot, chat_id, target, seconds):
        await notice(message, "❌ سکوت اعمال نشد؛ سطح دسترسی ربات یا وضعیت کاربر مناسب نیست.")
        return
    await _log_action(chat_id, target, f"mute:سکوت دستی:{seconds}s")
    await _drop_replied(message, bot)
    await notice(
        message,
        f"🔇 {_mention(target)} تا {humanize(seconds)} سکوت شد.",
    )
    modlog.notify(
        bot,
        chat_id,
        action=f"سکوت {humanize(seconds)}",
        target=target,
        actor=_actor(message),
        actor_source="admin",
        reason="سکوت دستی",
    )


async def _do_full_mute(message: Message, bot, target: int) -> None:
    """Permanent restriction: keeps the member, removes every send right."""
    chat_id = message.chat.id
    if not await moderation.perm_mute(bot, chat_id, target):
        await notice(message, "❌ سکوت دائم اعمال نشد؛ سطح دسترسی ربات یا وضعیت کاربر مناسب نیست.")
        return
    await _log_action(chat_id, target, "سکوت دائم")
    await _drop_replied(message, bot)
    await notice(message, f"🔇 {_mention(target)} برای همیشه سکوت شد.")
    modlog.notify(
        bot,
        chat_id,
        action="سکوت دائم",
        target=target,
        actor=_actor(message),
        actor_source="admin",
        reason="سکوت دائم",
    )


async def _do_unmute(
    message: Message, bot, target: int, target_name: str | None = None
) -> None:
    """Hand the default send rights back - the exact reverse of a permanent mute."""
    chat_id = message.chat.id
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=target,
            permissions=DEFAULT_SEND_PERMISSIONS,
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        await notice(
            message,
            "❌ لغو سکوت اعمال نشد؛ سطح دسترسی ربات یا وضعیت کاربر مناسب نیست.",
        )
        return
    await notice(
        message,
        f"🔊 سکوت کاربر {_mention(target, target_name)} با موفقیت لغو شد.",
        delay=7.0,
    )
    log.info("UNMUTE by %s on %s -> %s", _actor(message), chat_id, target)


async def _do_ban(message: Message, bot, target: int) -> None:
    chat_id = message.chat.id
    actor = message.from_user
    if actor is None:
        return

    # Moderation validation: only God, Deputy, Senior Chat, Senior Call may ban
    actor_role = await hierarchy.get_role(chat_id, actor.id)
    if actor_role not in {
        hierarchy.Role.GOD,
        hierarchy.Role.DEPUTY,
        hierarchy.Role.SENIOR_CHAT,
        hierarchy.Role.SENIOR_CALL,
    }:
        await notice(message, "❌ شما مجوز بن کردن ندارید.", delay=7.0)
        return

    if not await moderation.ban(bot, chat_id, target):
        await notice(message, "❌ بن اعمال نشد؛ سطح دسترسی ربات یا وضعیت کاربر مناسب نیست.")
        return
    await _log_action(chat_id, target, "بن دستی")
    await _drop_replied(message, bot)
    await notice(message, f"🚫 {_mention(target)} از گروه بن شد.")
    modlog.notify(
        bot,
        chat_id,
        action="بن",
        target=target,
        actor=_actor(message),
        actor_source="admin",
        reason="بن دستی",
    )


async def _do_warn(message: Message, bot, target: int, target_name: str | None = None) -> None:
    """Same ladder as the automatic guards.

    1st strike -> timed mute. Hitting the ceiling never bans: the member is
    muted forever and the assigned Level 2 (Owner) gets a private alert to
    make the final «بن» call.
    """
    chat_id = message.chat.id
    strike = await strikes.register(chat_id, target)

    if strike <= 1:
        if not await moderation.mute(bot, chat_id, target, WARN_MUTE):
            await notice(message, "❌ اخطار ثبت نشد؛ سطح دسترسی ربات مناسب نیست.")
            return
        await _log_action(chat_id, target, f"mute:اخطار دستی:{WARN_MUTE}s")
        await _drop_replied(message, bot)
        await notice(
            message,
            f"⚠️ اخطار ۱ برای {_mention(target)}\n"
            f"تا {humanize(WARN_MUTE)} سکوت است؛ "
            "اخطار بعدی = سکوت دائم.",
        )
        modlog.notify(
            bot,
            chat_id,
            action=f"اخطار {strike} — سکوت {humanize(WARN_MUTE)}",
            target=target,
            actor=_actor(message),
            actor_source="admin",
            reason="اخطار دستی",
        )
        return

    reason = f"اخطار دستی ({strike})"
    if not await moderation.perm_mute(bot, chat_id, target):
        await notice(message, "❌ اخطار اعمال نشد؛ سطح دسترسی ربات مناسب نیست.")
        return
    await _log_action(chat_id, target, f"سکوت دائم:{reason}")
    await _drop_replied(message, bot)
    await notice(
        message,
        f"🔇 اخطار {fa_num(strike)} برای {_mention(target)}\n"
        "به دلیل تکرار تخلف برای همیشه سکوت شد؛ "
        "تصمیم نهایی با مالک گروه است.",
    )
    modlog.notify(
        bot,
        chat_id,
        action=f"اخطار {strike} — سکوت دائم",
        target=target,
        actor=_actor(message),
        actor_source="admin",
        reason=reason,
    )
    modlog.escalate(
        bot,
        chat_id,
        target=target,
        reason=reason,
        target_name=target_name,
    )


# --------------------------------------------------------------- bulk wipe ---
async def _do_wipe(message: Message, bot) -> None:
    reply = message.reply_to_message
    if reply is None:
        await notice(
            message,
            "⚠️ برای پاکسازی، روی اولین پیامی که می‌خواهید حذف شود ریپلای کرده و "
            "کلمه «پاکسازی» را ارسال کنید.",
            delay=5.0,
        )
        return

    start = reply.message_id
    end = message.message_id
    if start > end:
        start, end = end, start

    deleted = 0
    for chunk_start in range(start, end + 1, 100):
        chunk = list(range(chunk_start, min(chunk_start + 100, end + 1)))
        try:
            ok = await bot.delete_messages(message.chat.id, chunk)
            if ok:
                deleted += len(chunk)
            else:
                # delete_messages returned False - at least one ID was invalid.
                # Fall back to one-by-one deletion for this chunk.
                for message_id in chunk:
                    if await moderation.safe_delete(bot, message.chat.id, message_id):
                        deleted += 1
        except Exception as exc:
            log.debug("wipe chunk failed (%s), falling back to one-by-one", exc)
            for message_id in chunk:
                if await moderation.safe_delete(bot, message.chat.id, message_id):
                    deleted += 1
        if chunk_start + 100 <= end:
            await asyncio.sleep(1.5)

    await notice(
        message,
        f"✅ {deleted} پیام تا این نقطه با موفقیت پاکسازی شدند.",
        delay=5.0,
    )
    log.info("WIPE by %s on %s from=%s to=%s deleted=%s", _actor(message), message.chat.id, start, end, deleted)


WIPE_ALL_DEPTH = 10000
WIPE_ALL_CHUNK = 100
WIPE_ALL_SLEEP = 0.05


async def _ask_wipe_all(message: Message) -> None:
    """Ask for explicit confirmation before the destructive full-history wipe."""
    try:
        sent = await message.answer(
            "⚠️ پاک‌سازی همه\n\n"
            f"این عملیات تا {fa_num(WIPE_ALL_DEPTH)} پیام اخیر را حذف می‌کند "
            "و قابل بازگشت نیست.\n\n"
            "آیا مطمئن هستید؟",
            reply_markup=wipe_confirm_kb(),
        )
    except Exception as exc:
        log.debug("wipe confirmation prompt failed: %s", exc)
        return
    spawn(delete_later(sent, 30.0), name="wipe-confirm-clean")


async def _do_wipe_all(message: Message, bot) -> None:
    """Delete history backwards from the current message down to message_id 1.

    Uses the Local Telegram Bot API (port 8081) to bypass the 48-hour cloud
    restriction. Message ids are collected and deleted in chunks of 100 via
    `delete_messages` (Telegram's per-request limit), which is far faster than
    the previous one-by-one loop. A failed chunk falls back to single deletes so
    the count stays as accurate as possible.
    """
    chat_id = message.chat.id
    current_id = message.message_id
    floor = max(1, current_id - WIPE_ALL_DEPTH)
    deleted_count = 0
    skipped_count = 0
    start_time = time.time()

    ids = list(range(current_id, floor - 1, -1))
    total = len(ids)

    log.info(
        "WIPE ALL START chat=%s current_id=%d floor=%d depth=%d total=%d",
        chat_id, current_id, floor, WIPE_ALL_DEPTH, total,
    )

    stop = False
    for offset in range(0, total, WIPE_ALL_CHUNK):
        if stop:
            break
        chunk = ids[offset:offset + WIPE_ALL_CHUNK]
        try:
            await bot.delete_messages(chat_id=chat_id, message_ids=chunk)
            deleted_count += len(chunk)
            log.debug(
                "WIPE ALL chunk deleted chat=%d ids=%d..%d",
                chat_id, chunk[0], chunk[-1],
            )
        except TelegramForbiddenError as exc:
            skipped_count += len(chunk)
            log.warning(
                "WIPE ALL forbidden chat=%d ids=%d..%d: %s",
                chat_id, chunk[0], chunk[-1], exc,
            )
            break  # bot lost admin rights, stop wiping
        except TelegramBadRequest as exc:
            log.debug(
                "WIPE ALL chunk failed chat=%d ids=%d..%d: %s; falling back",
                chat_id, chunk[0], chunk[-1], exc,
            )
        except Exception as exc:
            log.debug(
                "WIPE ALL chunk failed chat=%d ids=%d..%d: %s; falling back",
                chat_id, chunk[0], chunk[-1], exc,
            )
        else:
            # Small delay between successful batches to avoid FloodWait.
            await asyncio.sleep(WIPE_ALL_SLEEP)
            continue

        # Fallback: delete the failed chunk one-by-one for accurate counting.
        for message_id in chunk:
            try:
                if await bot.delete_message(chat_id, message_id):
                    deleted_count += 1
                else:
                    skipped_count += 1
            except TelegramForbiddenError as exc:
                skipped_count += 1
                log.warning(
                    "WIPE ALL forbidden chat=%d msg_id=%d: %s",
                    chat_id, message_id, exc,
                )
                stop = True  # bot lost admin rights, stop wiping
                break
            except TelegramBadRequest as exc:
                skipped_count += 1
                log.debug(
                    "WIPE ALL skipped chat=%d msg_id=%d: %s",
                    chat_id, message_id, exc,
                )
            except Exception as exc:
                skipped_count += 1
                log.debug(
                    "WIPE ALL skipped chat=%d msg_id=%d: %s",
                    chat_id, message_id, exc,
                )

        # Small delay to avoid FloodWait
        await asyncio.sleep(WIPE_ALL_SLEEP)

    elapsed = round(time.time() - start_time, 2)

    text = (
        f"✅ تاریخچه گروه با موفقیت پاک‌سازی شد.\n"
        f"🗑 تعداد: {deleted_count} پیام\n"
        f"⏱ زمان عملیات: {elapsed} ثانیه"
    )
    if skipped_count:
        text += f"\n⚠️ نادیده گرفته شد: {skipped_count} پیام (عدم دسترسی یا ادمین)"
    await notice(message, text, delay=15.0)
    log.info(
        "WIPE ALL COMPLETE chat=%s deleted=%d skipped=%d elapsed=%.2fs floor=%d current=%d",
        chat_id, deleted_count, skipped_count, elapsed, floor, current_id,
    )


async def _do_purge(message: Message, bot, args: str) -> None:
    chat_id = message.chat.id
    count = parse_count(args)
    start = message.message_id - 1
    ids = list(range(start, max(0, start - count), -1))
    if not ids:
        await notice(message, "🧹 پیامی برای پاکسازی وجود ندارد.")
        return
    try:
        done = await bot.delete_messages(chat_id, ids)
    except Exception as exc:
        log.debug("bulk purge failed (%s), falling back to one-by-one", exc)
        done = 0
        for message_id in ids:
            done += int(await moderation.safe_delete(bot, chat_id, message_id))
    await notice(
        message,
        f"🧹 پاکسازی انجام شد؛ {fa_num(count if done else 0)} پیام اخیر حذف شد.",
    )


# ----------------------------------------------------------------- handler ---
@router.message(ModTrigger())
async def on_mod_command(message: Message, bot: Bot) -> None:
    parsed = parse_mod_command(message.text)
    actor = message.from_user
    chat = message.chat
    if parsed is None or actor is None or chat is None or is_bot_message(message):
        return

    name, args = parsed
    role = await rbac.get_role(chat.id, actor.id)

    if not await _require_subscription(message, bot):
        return

    if role < Role.SUBADMIN:
        # only reachable through an explicit slash command - give feedback
        await _ack_delete(message, bot)
        await notice(message, "❌ فقط تیم مدیریت گروه می‌تواند از دستورات اعمال قانون استفاده کند.")
        return

    await _ack_delete(message, bot)

    if name == "ban" and role < Role.ADMIN:
        # rule §3: Level 1 may silence and warn, never remove a member.
        await notice(message, BAN_NOTICE)
        return

    if name == "wipe_all":
        await _ask_wipe_all(message)
        return

    if name == "wipe":
        await _do_wipe(message, bot)
        return

    if name == "purge":
        await _do_purge(message, bot, args)
        return

    target, rest = resolve_target(
        message.reply_to_message.from_user.id if message.reply_to_message else None,
        args,
    )
    if target is None:
        await notice(
            message,
            "⚠️ لطفاً روی پیام کاربر موردنظر ریپلای کنید.",
            delay=7.0,
        )
        return

    problem = await _check_target(bot, chat.id, actor.id, role, target)
    if problem is not None:
        await notice(message, problem)
        return

    if name == "mute":
        await _do_mute(message, bot, target, rest)
    elif name == "full_mute":
        await _do_full_mute(message, bot, target)
    elif name == "unmute":
        await _do_unmute(message, bot, target, _target_name(message, target))
    elif name == "ban":
        await _do_ban(message, bot, target)
    else:
        await _do_warn(message, bot, target, _target_name(message, target))

    log.info("MOD %s by %s on %s -> %s", name.upper(), actor.id, chat.id, target)


# --------------------------------------------------------- silent cleanup ---
@router.message(RemoveTrigger())
async def on_remove_command(message: Message, bot: Bot) -> None:
    """Delete the replied-to message, then the trigger itself - nothing else.

    No warning, no mute, no confirmation: this is a silent cleanup tool for
    Level 1-3 staff (rule §6 - the chat looks untouched afterwards). The
    filter already required the reply, the exact word and the staff rank; the
    checks below only keep the handler safe on its own.
    """
    actor = message.from_user
    chat = message.chat
    if actor is None or chat is None or is_bot_message(message):
        return
    if not await _require_subscription(message, bot):
        return
    if await rbac.get_role(chat.id, actor.id) < Role.SUBADMIN:
        return

    reply = message.reply_to_message
    removed: int | None = None
    if reply is not None and not is_bot_message(reply):
        # rule §9: a bot's own output (panel, notice) is never deleted here
        removed = reply.message_id
        await moderation.safe_delete(bot, chat.id, reply.message_id)

    await _ack_delete(message, bot)
    log.info("REMOVE by %s on %s -> msg %s", actor.id, chat.id, removed)


# ------------------------------------------------------------- pin / unpin ---
async def on_pin_command(message: Message, bot: Bot) -> None:
    """step 6: reply "پین" - pin the replied message (Clean Chat protocol)."""
    actor = message.from_user
    chat = message.chat
    if actor is None or chat is None or is_bot_message(message):
        return
    if not await _require_subscription(message, bot):
        return
    if await rbac.get_role(chat.id, actor.id) < Role.SUBADMIN:
        return
    reply = message.reply_to_message
    if reply is None:
        return

    await _ack_delete(message, bot)
    try:
        await bot.pin_chat_message(
            chat_id=chat.id, message_id=reply.message_id, disable_notification=False
        )
    except Exception as exc:
        log.debug("pin failed: %s", exc)
        await notice(message, "❌ پین انجام نشد (دسترسی پین برای ربات فعال نیست).", delay=7.0)
        return
    await notice(message, "📌 پیام با موفقیت پین شد.", delay=7.0)
    log.info("PIN by %s on %s -> msg %s", actor.id, chat.id, reply.message_id)


async def on_unpin_command(message: Message, bot: Bot) -> None:
    """step 6: "حذف پین" - unpin the replied message, or the latest pin."""
    actor = message.from_user
    chat = message.chat
    if actor is None or chat is None or is_bot_message(message):
        return
    if not await _require_subscription(message, bot):
        return
    if await rbac.get_role(chat.id, actor.id) < Role.SUBADMIN:
        return

    reply = message.reply_to_message
    await _ack_delete(message, bot)
    try:
        if reply is not None:
            await bot.unpin_chat_message(chat_id=chat.id, message_id=reply.message_id)
        else:
            await bot.unpin_chat_message(chat_id=chat.id)
    except Exception as exc:
        log.debug("unpin failed: %s", exc)
        await notice(message, "❌ برداشتن پین انجام نشد.", delay=7.0)
        return
    await notice(message, "📌 پین پیام برداشته شد.", delay=7.0)
    log.info("UNPIN by %s on %s", actor.id, chat.id)


@router.message(PinTrigger())
async def pin_entry(message: Message, bot: Bot) -> None:
    await on_pin_command(message, bot)


@router.message(UnpinTrigger())
async def unpin_entry(message: Message, bot: Bot) -> None:
    await on_unpin_command(message, bot)


# --------------------------------------------------------- temporary VIP ----
@router.message(VipTrigger())
async def on_vip_command(message: Message, bot: Bot) -> None:
    action = vip_action(message.text)
    reply = message.reply_to_message
    actor = message.from_user
    chat = message.chat
    target = reply.from_user if reply is not None else None
    if action is None or actor is None or chat is None or target is None:
        return
    if not await _require_subscription(message, bot):
        return

    await _ack_delete(message, bot)
    mention = _mention(target.id, target.full_name)

    if action == "revoke":
        await revoke_user_vip(chat.id, target.id)
        await notice(message, f"🚫 وضعیت ویژهٔ {mention} لغو شد.", delay=7.0)
        log.info("VIP REVOKE by %s on %s -> %s", actor.id, chat.id, target.id)
        return

    await set_user_vip(chat.id, target.id, VIP_TTL)
    await notice(
        message,
        f"🌟 کاربر {mention} به مدت ۳ ساعت کاربر ویژه شد و می‌تواند رسانه ارسال کند.",
        delay=7.0,
    )
    log.info("VIP GRANT by %s on %s -> %s", actor.id, chat.id, target.id)


# --------------------------------------------------------- hierarchical RBAC --
_PERSIAN_TO_LATIN = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)
_ARABIC_CHARS = str.maketrans("يك", "یک")


def _normalize_role_text(text: str | None) -> str:
    raw = (text or "").strip()
    if raw.startswith("/"):
        raw = raw[1:].strip()
    raw = raw.translate(_ARABIC_CHARS).translate(_PERSIAN_TO_LATIN)
    return re.sub(r"\s+", " ", raw)


_ROLE_ALIASES: tuple[tuple[str, str], ...] = tuple(
    sorted(
        (
            (_normalize_role_text(alias), role)
            for alias, role in hierarchy.PROMOTE_ALIASES.items()
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )
)


CUSTOM_TITLE_LIMIT = 16
BOT_MANAGED_TITLES = frozenset(hierarchy.CUSTOM_TITLES.values())



def _parse_role_command_details(
    text: str | None, *, has_reply: bool = False
) -> tuple[str, str, int | None] | None:
    """Parse a role command plus its optional explicit user id.

    Returns ``(command_text, target_role, explicit_user_id)``.
    A leading numeric token is accepted as an explicit Telegram id when the
    command is not a reply. Any trailing text after the command or ID is ignored.
    """
    raw = _normalize_role_text(text)
    if not raw:
        return None
    for alias, target_role in _ROLE_ALIASES:
        if raw == alias:
            return alias, target_role, None
        if raw.startswith(alias + " "):
            remainder = raw[len(alias):].strip()
            if not remainder:
                return alias, target_role, None
            first, _, _rest = remainder.partition(" ")
            if first.isdigit() and not has_reply:
                return alias, target_role, int(first)
            # With a reply, ignore trailing text (no custom title parsing).
            return alias, target_role, None
    return None


def _parse_role_command(
    text: str | None, *, has_reply: bool = False
) -> tuple[str, str, int | None] | None:
    """Backward-compatible parser returning the command, role and target id."""
    return _parse_role_command_details(text, has_reply=has_reply)


async def _effective_hierarchy_role(chat_id: int, user_id: int) -> str:
    """Merge the organizational rank with the operational RBAC rank.

    Group owners and God admins are not stored in the hierarchy until they are
    explicitly promoted, so using only ``hierarchy.get_role`` made every
    promotion command silently return for the people who own the group.
    """
    role = await hierarchy.get_role(chat_id, user_id)
    operational = await rbac.get_role(chat_id, user_id)
    mapped = {
        Role.GOD: hierarchy.Role.GOD,
        Role.ADMIN: hierarchy.Role.DEPUTY,
    }.get(operational, hierarchy.Role.USER)
    return role if hierarchy.level(role) >= hierarchy.level(mapped) else mapped


async def _do_promote(
    message: Message,
    bot: Bot,
    command_text: str,
    target_role: str,
    explicit_id: int | None = None,
) -> None:
    """Promote/demote a replied-to user or an explicitly supplied Telegram id."""
    chat_id = message.chat.id
    actor = message.from_user
    if actor is None or chat_id is None:
        return

    target_name: str | None = None
    if explicit_id is not None:
        target_id = explicit_id
    else:
        reply = message.reply_to_message
        target_user = reply.from_user if reply is not None else None
        if target_user is None:
            await notice(
                message,
                "❌ روی پیام کاربر ریپلای کنید یا شناسه عددی تلگرام را بعد از "
                "دستور بفرستید.",
                delay=7.0,
            )
            return
        target_id = target_user.id
        target_name = target_user.full_name

    if target_id == actor.id:
        await notice(message, "❌ نمی‌توانید مقام خودتان را تغییر دهید.", delay=7.0)
        return

    actor_role = await _effective_hierarchy_role(chat_id, actor.id)
    current_role = await hierarchy.get_role(chat_id, target_id)
    requested_role = target_role
    if requested_role == hierarchy.Role.USER:
        target_role = hierarchy.fallback_admin_role(current_role, command_text)
        if current_role == hierarchy.Role.ADMIN_CHAT_CALL:
            if target_role == hierarchy.Role.ADMIN_CALL:
                allowed = hierarchy.can_demote(
                    actor_role, hierarchy.Role.ADMIN_CHAT
                )
            elif target_role == hierarchy.Role.ADMIN_CHAT:
                allowed = hierarchy.can_demote(
                    actor_role, hierarchy.Role.ADMIN_CALL
                )
            else:
                allowed = hierarchy.can_demote(
                    actor_role, hierarchy.Role.ADMIN_CHAT_CALL
                )
        else:
            allowed = hierarchy.can_appoint(
                actor_role, hierarchy.Role.USER, current_role
            )
    else:
        target_role = hierarchy.merged_admin_role(current_role, requested_role)
        allowed = hierarchy.can_appoint(actor_role, requested_role, current_role)
    if not allowed:
        await notice(
            message,
            "❌ شما مجوز نصب/عزل این مقام را ندارید.",
            delay=7.0,
        )
        return

    # Telegram permissions first: a failed API call must not leave the Redis
    # hierarchy and the operational RBAC layer out of sync.
    perms = hierarchy.permissions_for(target_role)
    try:
        await bot.promote_chat_member(
            chat_id=chat_id,
            user_id=target_id,
            can_change_info=perms.get("can_change_info", False),
            can_delete_messages=perms.get("can_delete_messages", False),
            can_invite_users=perms.get("can_invite_users", False),
            can_restrict_members=perms.get("can_restrict_members", False),
            can_pin_messages=perms.get("can_pin_messages", False),
            can_promote_members=perms.get("can_promote_members", False),
            can_manage_video_chats=perms.get("can_manage_video_chats", False),
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.warning("promote_chat_member failed: %s", exc)
        await notice(
            message,
            f"❌ نصب مقام در تلگرام انجام نشد: <code>{html.escape(str(exc))}</code>",
            delay=7.0,
        )
        return
    except Exception:
        log.exception("promote_chat_member crashed")
        await notice(message, "❌ نصب مقام انجام نشد؛ خطای داخلی رخ داد.", delay=7.0)
        return


    try:
        if target_role == hierarchy.Role.USER:
            await hierarchy.remove_role(chat_id, target_id)
            await rbac.clear_operational_role(chat_id, target_id)
            result = "✅ مقام کاربر حذف شد و دسترسی عملیاتی او هم‌تراز شد."
        else:
            await hierarchy.set_role(chat_id, target_id, target_role)
            operational = HIERARCHY_OPERATIONAL_LEVEL.get(target_role)
            if operational is not None:
                await rbac.sync_operational_role(
                    chat_id, target_id, operational, granted_by=actor.id
                )
            if (
                current_role == hierarchy.Role.ADMIN_CHAT_CALL
                and target_role in hierarchy.ADMIN_ROLES
            ):
                result = (
                    "✅ یک بخش از دسترسی ادمین حذف شد و لقب و دسترسی "
                    "به مقام باقی‌مانده تغییر کرد."
                )
            else:
                result = "✅ کاربر به مقام منصوب شد، لقب و دسترسی عملیاتی دریافت کرد."
    except Exception:
        log.exception("role persistence failed")
        await notice(
            message,
            "⚠️ مقام در تلگرام اعمال شد، اما ذخیرهٔ داخلی انجام نشد. دوباره تلاش کنید.",
            delay=7.0,
        )
        return

    await notice(message, f"{result}\n👤 {_mention(target_id, target_name)}", delay=7.0)
    log.info(
        "ROLE PROMOTE by %s on %s -> %s in %s (%s)",
        actor.id, target_id, target_role, chat_id, command_text,
    )


async def _is_title_protected(chat_id: int, user_id: int) -> bool:
    """Check the manual-title lock marker stored in Redis."""
    try:
        return bool(await get_redis().exists(k_title_protected(chat_id, user_id)))
    except Exception:
        log.debug("title protection check failed", exc_info=True)
        return False


def _admin_roles_from_member(role: str, member) -> set[str]:
    """Infer Chat/Call Admin roles from hierarchy plus Telegram permissions."""
    if getattr(member, "status", None) == "creator":
        return set()

    roles: set[str] = set()
    if role == hierarchy.Role.ADMIN_CHAT:
        roles.add(hierarchy.Role.ADMIN_CHAT)
    elif role == hierarchy.Role.ADMIN_CALL:
        roles.add(hierarchy.Role.ADMIN_CALL)
    elif role == hierarchy.Role.ADMIN_CHAT_CALL:
        roles.update(hierarchy.ADMIN_ROLES)
    elif role != hierarchy.Role.USER:
        return set()

    if getattr(member, "can_delete_messages", False) or getattr(
        member, "can_invite_users", False
    ):
        roles.add(hierarchy.Role.ADMIN_CHAT)
    if getattr(member, "can_manage_video_chats", False) or getattr(
        member, "can_manage_voice_chats", False
    ):
        roles.add(hierarchy.Role.ADMIN_CALL)
    return roles


def _audit_target_title(roles: set[str]) -> str:
    if roles >= hierarchy.ADMIN_ROLES:
        return hierarchy.custom_title(hierarchy.Role.ADMIN_CHAT_CALL)
    if hierarchy.Role.ADMIN_CHAT in roles:
        return hierarchy.custom_title(hierarchy.Role.ADMIN_CHAT)
    if hierarchy.Role.ADMIN_CALL in roles:
        return hierarchy.custom_title(hierarchy.Role.ADMIN_CALL)
    return ""


async def _admin_target_title(chat_id: int, member) -> str:
    user = getattr(member, "user", None)
    if user is None or getattr(user, "is_bot", False):
        return ""
    role = await hierarchy.get_role(chat_id, user.id)
    return _audit_target_title(_admin_roles_from_member(role, member))


@router.message(AdminAuditTrigger())
async def on_admin_audit(message: Message, bot: Bot) -> None:
    """Audit group administrators and sync only bot-managed titles."""
    chat_id = message.chat.id
    actor = message.from_user
    if actor is None or chat_id is None or is_bot_message(message):
        return

    actor_role = await _effective_hierarchy_role(chat_id, actor.id)
    if hierarchy.level(actor_role) < hierarchy.level(hierarchy.Role.DEPUTY):
        await notice(message, "❌ شما مجوز بررسی ادمین‌ها را ندارید.", delay=7.0)
        return

    try:
        admins = list(await bot.get_chat_administrators(chat_id))
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.warning("get_chat_administrators failed: %s", exc)
        await notice(message, "❌ دریافت فهرست ادمین‌ها انجام نشد.", delay=7.0)
        return
    except Exception:
        log.exception("get_chat_administrators crashed")
        await notice(message, "❌ دریافت فهرست ادمین‌ها انجام نشد.", delay=7.0)
        return

    checked = len(admins)
    updated = preserved = failed = 0
    for member in admins:
        user = getattr(member, "user", None)
        if user is None or getattr(user, "is_bot", False):
            continue

        current_title = (getattr(member, "custom_title", None) or "").strip()
        if await _is_title_protected(chat_id, user.id):
            preserved += 1
            continue
        if current_title and current_title not in BOT_MANAGED_TITLES:
            preserved += 1
            continue

        target_title = await _admin_target_title(chat_id, member)
        if not target_title or current_title == target_title:
            continue

        try:
            await bot.set_chat_administrator_custom_title(
                chat_id=chat_id,
                user_id=user.id,
                custom_title=target_title,
            )
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            failed += 1
            log.warning(
                "admin audit title update failed user=%s: %s", user.id, exc
            )
        except Exception:
            failed += 1
            log.exception("admin audit title update crashed user=%s", user.id)
        else:
            updated += 1
            log.info(
                "ADMIN AUDIT title synced chat=%s user=%s title=%s",
                chat_id,
                user.id,
                target_title,
            )

    report = (
        "🔎 <b>گزارش بررسی ادمین‌ها</b>\n"
        f"👥 بررسی‌شده: {checked}\n"
        f"✏️ به‌روزرسانی‌شده: {updated}\n"
        f"🛡 عناوین سفارشی حفظ‌شده: {preserved}"
    )
    if failed:
        report += f"\n⚠️ خطا: {failed}"
    await notice(message, report, delay=30.0)


@router.message(RoleCommandTrigger())
async def on_role_command(message: Message, bot: Bot) -> None:
    """Handle promotion/demotion commands (نصب/عزل معاون, ارشد چت, ...)."""
    parsed = _parse_role_command_details(
        message.text, has_reply=message.reply_to_message is not None
    )
    if parsed is None:
        return
    chat_id = message.chat.id
    actor = message.from_user
    if actor is None or chat_id is None:
        return

    # Group owners must be able to bootstrap the hierarchy: they have an
    # operational Level-2 role but no stored hierarchical role yet.
    operational = await rbac.get_role(chat_id, actor.id)
    if operational < Role.ADMIN:
        actor_role = await hierarchy.get_role(chat_id, actor.id)
        if hierarchy.level(actor_role) < hierarchy.level(hierarchy.Role.ADMIN_CALL):
            return

    command_text, target_role, explicit_id = parsed
    await _do_promote(
        message,
        bot,
        command_text,
        target_role,
        explicit_id,
    )
