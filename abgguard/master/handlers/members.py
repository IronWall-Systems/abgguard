from __future__ import annotations

import asyncio
import html
import json
import logging
import random

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Filter
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from abgguard.config import settings
from abgguard.core import botpair, rbac, settings_store
from abgguard.core import groups as groups_store
from abgguard.core import redis as rk
from abgguard.core.rbac import Role
from abgguard.master import ui
from abgguard.master.ephemeral import delete_later
from abgguard.models.entities import GroupSubscription
from abgguard.services import antiflood, subscription
from abgguard.services.moderation import temp_mute_chat

log = logging.getLogger("abgguard.master.members")

router = Router(name="master.members")
captcha_router = Router(name="master.captcha")

_ME_ID: int | None = None


async def _master_id(bot: Bot) -> int:
    global _ME_ID
    if _ME_ID is None:
        try:
            _ME_ID = (await bot.me()).id
        except Exception:
            _ME_ID = 0
    return _ME_ID

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
            sent = await bot.send_message(
                chat_id,
                "🚨 موج عضویت شناسایی شد!\n"
                "گروه موقتاً برای جلوگیری از تبلیغات قفل شد "
                f"({settings.join_lock_seconds} ثانیه).\n"
                "هیچ کاربری بن نمی‌شود.",
            )
            _spawn(delete_later(sent, 30), name=f"join-alert:{chat_id}")
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


# ================================================ SaaS onboarding (my_chat_member) ==
async def _dual_bot_state(bot: Bot, chat_id: int) -> tuple[bool, bool]:
    """(present, is_admin) for @ABGGUARD1BOT inside this chat."""
    present = await botpair.worker_present(chat_id)
    is_admin = present and await botpair.worker_is_admin(chat_id)
    return present, is_admin


async def _send_card(bot: Bot, chat_id: int, text: str, kb=None) -> None:
    try:
        await bot.send_message(chat_id, text, reply_markup=kb)
    except Exception:
        log.debug("subscription card failed chat=%s", chat_id, exc_info=True)


async def _is_telegram_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    if getattr(member.user, "is_bot", False):
        return False
    return member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)


async def _resolve_group_admin(bot: Bot, event: ChatMemberUpdated) -> int | None:
    """Adding actor first; otherwise the group creator/first human admin."""
    adder = event.from_user
    if (
        adder is not None
        and not adder.is_bot
        and await _is_telegram_admin(bot, event.chat.id, adder.id)
    ):
        return adder.id

    try:
        admins = await bot.get_chat_administrators(event.chat.id)
    except Exception:
        return None
    creator: int | None = None
    fallback: int | None = None
    for member in admins:
        user = member.user
        if user.is_bot:
            continue
        if member.status == ChatMemberStatus.CREATOR:
            creator = user.id
        elif fallback is None:
            fallback = user.id
    return creator or fallback


async def _auto_bind_owner(bot: Bot, event: ChatMemberUpdated) -> None:
    """First admin who adds the bot becomes Level 2 owner (unless one exists)."""
    chat_id = event.chat.id
    if await rbac.list_owner_ids(chat_id):
        return
    user_id = await _resolve_group_admin(bot, event)
    if user_id is None or await rbac.is_god(user_id):
        return
    try:
        await rbac.add_admin(chat_id, user_id, granted_by=settings.god_admin_id)
    except Exception:
        log.warning("auto owner bind failed chat=%s user=%s", chat_id, user_id, exc_info=True)
        return
    log.info("AUTO OWNER bound: chat=%s user=%s", chat_id, user_id)


async def _onboard(bot: Bot, chat_id: int, title: str | None) -> None:
    """Spec §2: trial for 50+ members, payment card otherwise."""
    row = await subscription.record_join(chat_id, title)

    # whitelisted / paid groups stay COMPLETELY silent on (re)join: no
    # subscription card, no pricing, no tutorial - ever (fix directive #2)
    if row.status in (GroupSubscription.MANUAL_WHITELIST, GroupSubscription.ACTIVE):
        return

    present, is_admin = await _dual_bot_state(bot, chat_id)
    try:
        count = await bot.get_chat_member_count(chat_id)
    except Exception:
        count = 0

    if count >= subscription.TRIAL_MIN_MEMBERS and not row.trial_used:
        await subscription.activate_trial(chat_id)
        _spawn(
            _send_card(
                bot,
                chat_id,
                ui.trial_activated(
                    chat_id, title or str(chat_id),
                    subscription.TRIAL_HOURS, present, is_admin,
                ),
                ui.onboarding_kb(chat_id, back="abg:back"),
            ),
            name=f"onboard-trial:{chat_id}",
        )
        return

    text, kb = ui.subscription_required(
        chat_id, title or str(chat_id), count, present, is_admin, back="abg:back"
    )
    _spawn(_send_card(bot, chat_id, text, kb), name=f"onboard-unpaid:{chat_id}")


@router.my_chat_member()
async def on_master_membership(event: ChatMemberUpdated, bot: Bot) -> None:
    """The master itself was added / kicked - drives the whole SaaS lifecycle."""
    chat = event.chat
    if chat.type not in {"group", "supergroup"}:
        return
    me_id = await _master_id(bot)
    if event.new_chat_member.user.id != me_id:
        return  # other members join/leave via the chat_member path

    if event.new_chat_member.status in _JOINING:
        title = getattr(chat, "title", None)
        await groups_store.upsert_group(chat.id, title=title, username=chat.username)
        await rk.presence_add(chat.id)
        await _auto_bind_owner(bot, event)
        await _onboard(bot, chat.id, title)
        return

    if event.new_chat_member.status in _LEFT:
        await rk.presence_remove(chat.id)


@router.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.text.func(lambda t: t and t.split(maxsplit=1)[0].split("@", 1)[0] == "/sub"),
)
async def on_sub_command(message: Message, bot: Bot) -> None:
    """/sub in the group -> the current subscription status card."""
    chat_id = message.chat.id
    try:
        await message.delete()
    except Exception:
        pass

    row = await subscription.record_join(chat_id, message.chat.title)
    if not await subscription.is_active(chat_id):
        await subscription.send_expiry_notice(bot, chat_id)
        return

    present, is_admin = await _dual_bot_state(bot, chat_id)
    status = GroupSubscription.ACTIVE
    exp = row.expires_at
    text = ui.active_state_text(
        chat_id, message.chat.title or str(chat_id), status, exp, present, is_admin
    )
    kb = ui.subscribe_kb(chat_id, paid=ui.is_premium(status, exp), back="abg:back")
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("sub:"))
async def on_sub_callback(cq: CallbackQuery, bot: Bot) -> None:
    """Renewal/status refresh, works in the group AND in the bot's PV."""
    parts = (cq.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action in {"approve", "reject"} and len(parts) == 4:
        await _god_receipt_decision(cq, bot, action, int(parts[2]), int(parts[3]))
        return
    if len(parts) < 3 or action not in {"menu", "pay", "receipt"}:
        await cq.answer()
        return
    try:
        chat_id = int(parts[2])
    except ValueError:
        await cq.answer()
        return
    if cq.message is None or cq.message.chat is None:
        await cq.answer()
        return

    row = await subscription.record_join(chat_id)
    active = await subscription.is_active(chat_id)
    title = row.title or str(chat_id)
    back = "pv:substat" if cq.message.chat.type == "private" else "abg:back"
    if action == "pay":
        # dedicated payment screen - works in groups AND in the bot's PV
        await cq.message.answer(
            ui.pay_screen_text(chat_id, title, row.status, row.expires_at),
            reply_markup=await ui.pay_screen_kb(chat_id),
        )
        await cq.answer()
        return
    if action == "receipt":
        # in-bot receipt workflow: open a 15-minute submission window
        await rk.get_redis().set(
            f"{rk.P}:receipt_wait:{cq.from_user.id}", str(chat_id), ex=900
        )
        if cq.message.chat.type == "private":
            where = "همین‌جا در چت ربات"
        else:
            where = f"در چت خصوصی ربات @{settings.master_username}"
        await cq.message.answer(
            f"📩 لطفاً فیش پرداخت (عکس، فایل یا متن تراکنش) را {where} "
            "بفرستید.\n⏳ ۱۵ دقیقه فرصت دارید."
        )
        await cq.answer()
        return
    if not active:
        past, is_admin = (False, False)
        if cq.message.chat.type != "private":
            past, is_admin = await _dual_bot_state(bot, chat_id)
        text, kb = ui.subscription_required(
            chat_id, title, 0, past, is_admin, back=back
        )
    else:
        text = ui.pay_view_text(chat_id, title, GroupSubscription.ACTIVE, row.expires_at)
        kb = ui.subscribe_kb(chat_id, paid=True, back=back)

    try:
        await cq.message.answer(text, reply_markup=kb)
    except Exception:
        log.debug("sub card send failed chat=%s", chat_id, exc_info=True)
    await cq.answer()


async def _god_receipt_decision(
    cq: CallbackQuery, bot: Bot, action: str, chat_id: int, user_id: int
) -> None:
    """One-click receipt approval for the God Admin (directive 2)."""
    god = cq.from_user
    if god is None or not await rbac.is_god(god.id):
        await cq.answer("⛔️ فقط مدیریت کل (God Admin) می‌تواند فیش را تصویب کند.", show_alert=True)
        return
    titles = await groups_store.known_titles([chat_id])
    title = titles.get(chat_id, str(chat_id))
    if action == "approve":
        row = await subscription.activate_paid(chat_id)
        try:
            await cq.message.edit_text(
                f"✅ پرداخت تایید شد\n🏘 گروه: <b>{title}</b>\n"
                f"🆔 <code>{chat_id}</code>\n"
                f"📅 وضعیت: {ui.status_line(row.status, row.expires_at)}\n"
                f"👤 کاربر: <code>{user_id}</code>"
            )
        except Exception as exc:
            log.debug("god approve card edit failed: %s", exc)
        await cq.answer("اشتراک فعال شد ✅")
        try:
            await bot.send_message(
                user_id,
                f"✅ پرداخت شما تایید شد و اشتراک ۳۰ روزه گروه از این پس فعال است.\n"
                f"🏘 گروه: <b>{title}</b>",
            )
        except Exception:
            log.debug("payer DM failed chat=%s user=%s", chat_id, user_id, exc_info=True)
        try:
            await bot.send_message(
                chat_id,
                "🎉 پرداخت شما تایید شد و اشتراک گروه به مدت ۳۰ روز با موفقیت فعال گردید.",
            )
        except Exception:
            log.debug("group receipt notice failed chat=%s", chat_id, exc_info=True)
        log.warning("RECEIPT APPROVED chat=%s user=%s by=%s", chat_id, user_id, god.id)
        return
    try:
        await cq.message.edit_text(
            f"❌ فیش رد شد\n🏘 گروه: <b>{title}</b>\n"
            f"🆔 <code>{chat_id}</code>\n👤 کاربر: <code>{user_id}</code>"
        )
    except Exception as exc:
        log.debug("god reject card edit failed: %s", exc)
    await cq.answer("فیش رد شد")
    try:
        await bot.send_message(
            user_id,
            "❌ فیش پرداخت شما رد شد. جهت بررسی بیشتر با پشتیبانی (@pyyple) "
            "در ارتباط باشید.",
        )
    except Exception:
        log.debug("reject DM failed user=%s", user_id, exc_info=True)
    log.warning("RECEIPT REJECTED chat=%s user=%s by=%s", chat_id, user_id, god.id)


# ================================================ welcome + anti-bot CAPTCHA ==
_background: set[asyncio.Task] = set()


def _spawn(coro, *, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


DEFAULT_WELCOME = "👋 خوش آمدی {mention}!"
JOIN_CAPTCHA_DELAY = 5.0
_DIGITS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)


def _k_captcha(chat_id: int, user_id: int) -> str:
    return f"{rk.P}:captcha:{chat_id}:{user_id}"


def _k_verified(chat_id: int) -> str:
    return f"{rk.P}:verified:{chat_id}"


def _k_join_delay(chat_id: int, user_id: int) -> str:
    return f"{rk.P}:join_delay:{chat_id}:{user_id}"


async def _is_join_captcha_pending(chat_id: int, user_id: int) -> bool:
    return bool(await rk.get_redis().exists(_k_join_delay(chat_id, user_id)))


def _math_challenge() -> tuple[str, int]:
    """Return a simple whole-number question and its exact answer."""
    operation = random.choice(("*", "-", "/"))
    if operation == "*":
        left = random.randint(1, 10)
        right = random.randint(1, 10)
        return f"{left} * {right} = ?", left * right
    if operation == "-":
        high, low = sorted(random.sample(range(1, 11), 2), reverse=True)
        return f"{high} - {low} = ?", high - low
    divisor = random.randint(2, 10)
    answer = random.randint(1, 10 // divisor)
    return f"{divisor * answer} / {divisor} = ?", answer


def _normalize_answer(text: str | None) -> str:
    return (text or "").strip().translate(_DIGITS)


async def _get_captcha_state(chat_id: int, user_id: int) -> dict | None:
    raw = await rk.get_redis().get(_k_captcha(chat_id, user_id))
    if not raw:
        return None
    try:
        state = json.loads(raw)
    except (TypeError, ValueError):
        await rk.get_redis().delete(_k_captcha(chat_id, user_id))
        return None
    return state if isinstance(state, dict) else None


async def _is_unverified(chat_id: int, user_id: int) -> bool:
    return await _get_captcha_state(chat_id, user_id) is not None


async def _is_verified(chat_id: int, user_id: int) -> bool:
    return bool(await rk.get_redis().sismember(_k_verified(chat_id), str(user_id)))


async def _mark_verified(chat_id: int, user_id: int) -> None:
    await rk.get_redis().sadd(_k_verified(chat_id), str(user_id))


class UnverifiedFilter(Filter):
    """Matches only members who still have an active math challenge."""

    async def __call__(self, message: Message) -> bool:
        if message.chat.type not in {"group", "supergroup"}:
            return False
        if getattr(message, "new_chat_members", None) or getattr(
            message, "left_chat_member", None
        ):
            return False
        user = message.from_user
        if user is None or user.is_bot:
            return False
        return await _is_unverified(message.chat.id, user.id)


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
    """Count the member, run the math CAPTCHA when enabled, then welcome them."""
    r = rk.get_redis()
    pipe = r.pipeline(transaction=False)
    pipe.pfadd(rk.STATS_MEMBERS, f"{chat_id}:{user.id}")
    pipe.pfadd(rk.k_stats_seen(chat_id), str(user.id))
    await pipe.execute()

    # A join-request approval already owns the delayed CAPTCHA task. Without
    # this guard the chat_member update would start the challenge immediately
    # and bypass the required 5-second delay.
    if await _is_join_captcha_pending(chat_id, user.id):
        return

    policy = await settings_store.get_policy(chat_id)
    needs_captcha = (
        policy.captcha_enabled
        and await rbac.get_role(chat_id, user.id) < Role.SUBADMIN
        and not await _is_verified(chat_id, user.id)
    )
    if needs_captcha and await _start_captcha(bot, chat_id, user):
        return  # the welcome is delivered once the CAPTCHA is answered

    if policy.welcome_text:
        await _send_welcome(bot, chat_id, user, policy.welcome_text)


async def _send_welcome(bot: Bot, chat_id: int, user, template: str) -> None:
    try:
        sent = await bot.send_message(
            chat_id, _render_welcome(template, user, chat_id)
        )
    except Exception:
        log.debug("welcome failed chat=%s", chat_id, exc_info=True)
        return
    _spawn(
        delete_later(sent, 30),
        name=f"welcome-del:{chat_id}:{sent.message_id}",
    )


async def _start_captcha(bot: Bot, chat_id: int, user) -> bool:
    """Ask a one-answer math challenge.

    The user is NOT restricted - they must be free to type their numeric
    answer. Returns False when the prompt could not be sent, so the caller
    can fall back to a plain welcome.
    """
    r = rk.get_redis()
    key = _k_captcha(chat_id, user.id)
    lock = f"{key}:lock"
    if await r.exists(key):
        return True
    if not await r.set(lock, "1", nx=True, ex=15):
        return True

    try:
        if await r.exists(key):
            return True

        question, answer = _math_challenge()
        try:
            prompt = await bot.send_message(
                chat_id,
                "🛡 <b>تایید هویت</b>\n"
                f"{_render_welcome('{mention}', user, chat_id)}\n\n"
                "برای ارسال پیام، پاسخ عبارت ریاضی زیر را به صورت عدد صحیح بفرستید:\n\n"
                f"<code>{question}</code>\n\n"
                f"⏱ {settings.captcha_timeout} ثانیه",
            )
        except Exception:
            log.debug("captcha prompt failed chat=%s", chat_id, exc_info=True)
            return False

        state = {"answer": answer, "prompt_id": prompt.message_id}
        await r.set(
            key,
            json.dumps(state),
            ex=settings.captcha_timeout + 10,
        )
        _spawn(
            _captcha_expiry(bot, chat_id, user.id, prompt.message_id),
            name=f"captcha:{chat_id}:{user.id}",
        )
        _spawn(
            delete_later(prompt, settings.captcha_timeout + 10),
            name=f"captcha-del:{chat_id}:{user.id}",
        )
        return True
    finally:
        await r.delete(lock)


async def _captcha_expiry(bot: Bot, chat_id: int, user_id: int, prompt_id: int) -> None:
    """No verification in time -> just delete the prompt (never ban)."""
    await asyncio.sleep(settings.captcha_timeout)
    r = rk.get_redis()
    if not await r.delete(_k_captcha(chat_id, user_id)):
        return  # already verified
    try:
        await bot.delete_message(chat_id, prompt_id)
    except Exception:
        pass


async def _complete_captcha(
    bot: Bot, chat_id: int, user, answer_message_id: int, state: dict
) -> None:
    r = rk.get_redis()
    if not await r.delete(_k_captcha(chat_id, user.id)):
        return

    prompt_id = state.get("prompt_id")
    for message_id in (prompt_id, answer_message_id):
        if isinstance(message_id, int):
            try:
                await bot.delete_message(chat_id, message_id)
            except Exception:
                pass

    try:
        await bot.restrict_chat_member(chat_id, user.id, permissions=FULL_SEND)
    except Exception:
        log.debug("captcha lift failed chat=%s", chat_id, exc_info=True)

    await _mark_verified(chat_id, user.id)
    policy = await settings_store.get_policy(chat_id)
    await _send_welcome(
        bot, chat_id, user, policy.welcome_text or DEFAULT_WELCOME
    )
    log.info("CAPTCHA passed chat=%s user=%s", chat_id, user.id)


async def _delayed_join_captcha(bot: Bot, chat_id: int, user) -> None:
    """Wait 5 seconds after approval, then challenge the new member."""
    await asyncio.sleep(JOIN_CAPTCHA_DELAY)
    r = rk.get_redis()
    await r.delete(_k_join_delay(chat_id, user.id))

    if (
        await rbac.get_role(chat_id, user.id) >= Role.SUBADMIN
        or await _is_verified(chat_id, user.id)
    ):
        policy = await settings_store.get_policy(chat_id)
        await _send_welcome(
            bot, chat_id, user, policy.welcome_text or DEFAULT_WELCOME
        )
        return

    if await _start_captcha(bot, chat_id, user):
        return

    log.warning(
        "delayed join captcha failed chat=%s user=%s", chat_id, user.id
    )
    policy = await settings_store.get_policy(chat_id)
    await _send_welcome(bot, chat_id, user, policy.welcome_text or DEFAULT_WELCOME)


@captcha_router.chat_join_request()
async def on_join_request(request: ChatJoinRequest, bot: Bot) -> None:
    """Approve every join request immediately, then delay the CAPTCHA 5s."""
    chat = request.chat
    user = request.from_user
    if chat.type not in {"group", "supergroup"} or user is None:
        return

    r = rk.get_redis()
    await r.set(_k_join_delay(chat.id, user.id), "1", ex=15)
    try:
        await bot.approve_chat_join_request(chat.id, user.id)
    except Exception:
        await r.delete(_k_join_delay(chat.id, user.id))
        log.warning(
            "join request approve failed chat=%s user=%s",
            chat.id, user.id, exc_info=True,
        )
        return

    log.info("JOIN REQUEST approved chat=%s user=%s", chat.id, user.id)
    _spawn(
        _delayed_join_captcha(bot, chat.id, user),
        name=f"join-captcha:{chat.id}:{user.id}",
    )


@captcha_router.message(UnverifiedFilter())
async def on_unverified_message(message: Message, bot: Bot) -> None:
    """Delete every message from an unverified member except the exact answer."""
    user = message.from_user
    if user is None:
        return
    chat_id = message.chat.id
    state = await _get_captcha_state(chat_id, user.id)
    if state is None:
        return

    expected = state.get("answer")
    answer = _normalize_answer(message.text)
    if (
        isinstance(expected, int)
        and answer.isdigit()
        and int(answer) == expected
    ):
        await _complete_captcha(
            bot, chat_id, user, message.message_id, state
        )
        return

    try:
        await message.delete()
    except Exception:
        log.debug(
            "unverified message delete failed chat=%s user=%s",
            chat_id, user.id, exc_info=True,
        )

    try:
        warning = await bot.send_message(
            chat_id,
            "لطفاً اول کپچا (تایید هویت انسان) را با ارسال عدد صحیح حل کنید.",
        )
        _spawn(
            delete_later(warning, 12),
            name=f"captcha-warn:{chat_id}:{user.id}",
        )
    except Exception:
        log.debug(
            "captcha warning failed chat=%s user=%s",
            chat_id, user.id, exc_info=True,
        )


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
    await _mark_verified(chat_id, user_id)

    policy = await settings_store.get_policy(chat_id)
    await _send_welcome(
        bot, chat_id, actor, policy.welcome_text or DEFAULT_WELCOME
    )
