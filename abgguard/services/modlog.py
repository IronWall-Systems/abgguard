"""Private moderation log - every sanction also lands in the admins' DMs.

Rule 6 keeps the group itself clean: a sanction is announced in the chat by a
single ephemeral notice. The durable, per-admin record travels privately so
the team can audit who did what without polluting the group.

Delivery rules (mirrors §7): the fan-out is always a fire-and-forget task, it
never raises into the caller and it never blocks message handling. A recipient
who blocked the bot simply does not get the card.
"""

from __future__ import annotations

import asyncio
import html
import logging

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardMarkup

from abgguard.config import settings
from abgguard.core import groups, rbac
from abgguard.master import ui

log = logging.getLogger("abgguard.modlog")

PARSE_MODE = "HTML"
MAX_RECIPIENTS = 30  # a pathological roster must not fan out forever
FLOOD_CAP = 5.0  # seconds we are willing to wait for Telegram's RetryAfter

# Who executed the sanction. The card must always name a concrete source -
# "some bot did something" is not an audit trail.
#   admin    -> a human admin typed the command (actor carries their id)
#   master   -> automatic guard running in the Master bot
#   watchdog -> automatic guard running in the Shadow Watchdog bot
ACTOR_SOURCES: dict[str, str] = {
    "admin": "ادمین",
    "master": "ربات اصلی (Master)",
    "watchdog": "ربات نگهبان (Watchdog)",
}
UNKNOWN_ACTOR = "سیستم/ربات"

# strong references so scheduled fan-outs are never garbage collected mid-flight
_tasks: set[asyncio.Task] = set()

# The Master bot (@abgguardbot) owns every private audit card. A worker-side
# fallback handler may pass its own Bot instance; this binding keeps delivery on
# the Master token regardless of who produced the sanction.
_master_bot: Bot | None = None


def bind_master_bot(bot: Bot) -> None:
    global _master_bot
    _master_bot = bot


def _delivery_bot(bot: Bot | None) -> Bot | None:
    return _master_bot or bot


async def recipients(chat_id: int, *, extra: tuple[int, ...] = ()) -> list[int]:
    """Assigned admins of the group: Level 2 owners first, then Level 1 subs."""
    try:
        owners = await rbac.list_admins(chat_id)
        subs = await rbac.list_subadmins(chat_id)
    except Exception:
        log.exception("mod-log roster unavailable chat=%s", chat_id)
        owners, subs = [], []
    ordered = list(dict.fromkeys([*owners, *subs, *extra]))
    if len(ordered) > MAX_RECIPIENTS:
        log.warning("mod-log roster capped chat=%s n=%s", chat_id, len(ordered))
        ordered = ordered[:MAX_RECIPIENTS]
    return ordered


def actor_label(actor: int | None, actor_source: str | None) -> str:
    """Human-readable executor: a named bot, an admin id, or a last-resort tag."""
    source = (actor_source or "").strip().lower()
    if source in {"master", "watchdog"}:
        return ACTOR_SOURCES[source]
    if actor is not None:
        suffix = f" ({ACTOR_SOURCES['admin']})" if source == "admin" else ""
        return f"<code>{actor}</code>{suffix}"
    if source == "admin":
        return ACTOR_SOURCES["admin"]
    return UNKNOWN_ACTOR


def render(
    *,
    chat_id: int,
    action: str,
    target: int,
    actor: int | None = None,
    actor_source: str | None = None,
    reason: str = "",
) -> str:
    """The Persian mod-log card (rule 8: HTML-escaped, self-contained)."""
    lines = [
        "📜 لاگ اقدام امنیتی",
        "",
        f"🏘 گروه: <code>{chat_id}</code>",
        f"👤 اجراکننده: {actor_label(actor, actor_source)}",
        f"🎯 هدف: <code>{target}</code>",
        f"🔹 اقدام: {html.escape(action)}",
    ]
    if reason:
        lines.append(f"ℹ️ دلیل: {html.escape(reason)}")
    return "\n".join(lines)


async def resolve_group_title(bot: Bot, chat_id: int) -> str:
    """Human readable group name for the report card, with a safe fallback."""
    try:
        cached = await groups.known_titles([chat_id])
        if cached.get(chat_id):
            return str(cached[chat_id])
    except Exception:
        log.debug("group title cache miss chat=%s", chat_id, exc_info=True)
    try:
        chat = await bot.get_chat(chat_id)
        title = getattr(chat, "title", None) or getattr(chat, "full_name", None)
        if title:
            return str(title)
    except Exception:
        log.debug("group title lookup failed chat=%s", chat_id, exc_info=True)
    return str(chat_id)


def _username_line(username: str | None) -> str:
    return f"@{html.escape(username.lstrip('@'))}" if username else "—"


def render_report(
    *,
    group_title: str,
    target: int,
    action: str,
    reason: str,
    target_name: str | None = None,
    target_username: str | None = None,
    message_text: str | None = None,
    strike_count: int | None = None,
    action_taken: str | None = None,
) -> str:
    """The content-violation report: names the Telegram username + numeric id."""
    label = html.escape(target_name) if target_name else str(target)
    lines = [
        "🚨 گزارش اعمال قانون (تخلف محتوا)",
        "",
        f"👥 گروه: {html.escape(str(group_title))}",
        f'👤 کاربر متخلف: <a href="tg://user?id={target}">{label}</a>',
        f"🔗 آیدی تلگرام: {_username_line(target_username)}",
        f"🆔 آیدی عددی: <code>{target}</code>",
        f"⚖️ اقدام ربات: {html.escape(action)}",
        f"📝 علت: {html.escape(reason)}",
    ]
    if strike_count is not None:
        lines.append(f"⚠️ تعداد تخلف امروز: {strike_count}")
    if action_taken:
        lines.append(f"⚖️ اقدام ربات: {html.escape(action_taken)}")
    if message_text:
        lines.append(f"💬 متن اصلی پیام: {html.escape(message_text)}")
    return "\n".join(lines)


async def _send(bot: Bot, user_id: int, text: str) -> bool:
    """One DM; blocked users / dead chats are a normal outcome, not an error."""
    for attempt in (0, 1):
        try:
            await bot.send_message(user_id, text, parse_mode=PARSE_MODE)
            return True
        except TelegramRetryAfter as exc:
            if attempt:
                log.debug("mod-log flood-raced out user=%s", user_id)
                return False
            await asyncio.sleep(min(float(exc.retry_after), FLOOD_CAP))
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            log.debug("mod-log undeliverable user=%s (%s)", user_id, exc)
            return False
        except Exception:
            log.warning("mod-log delivery failed user=%s", user_id, exc_info=True)
            return False
    return False


async def deliver(
    bot: Bot | None,
    chat_id: int,
    *,
    action: str,
    target: int,
    actor: int | None = None,
    actor_source: str | None = None,
    reason: str = "",
    roster: list[int] | None = None,
    template: str = "standard",
    target_name: str | None = None,
    target_username: str | None = None,
    message_text: str | None = None,
    strike_count: int | None = None,
    action_taken: str | None = None,
) -> int:
    """Send the card to every assigned admin; returns how many got it.

    Never raises: a broken roster or a dead recipient only lowers the count.
    ``template="content"`` switches to the content-violation report, which is
    the only card that carries the offender's Telegram username + numeric id.
    """
    bot = _delivery_bot(bot)
    roster_ids = list(roster) if roster is not None else await recipients(chat_id)
    # God Admin always receives the private card (oversight of every group).
    ids = list(dict.fromkeys([settings.god_admin_id, *roster_ids]))
    if not ids or bot is None:
        return 0

    if template == "content":
        text = render_report(
            group_title=await resolve_group_title(bot, chat_id),
            target=target,
            action=action,
            reason=reason,
            target_name=target_name,
            target_username=target_username,
            message_text=message_text,
            strike_count=strike_count,
            action_taken=action_taken,
        )
    else:
        text = render(
            chat_id=chat_id,
            action=action,
            target=target,
            actor=actor,
            actor_source=actor_source,
            reason=reason,
        )

    # Inline action buttons for the God Admin / Level 2 owners on content cards.
    markup: InlineKeyboardMarkup | None = None
    if template == "content":
        markup = ui.markup(
            [
                ui.btn_cb(
                    "🔊 لغو سکوت (Unmute)",
                    f"modact:unmute:{chat_id}:{target}",
                ),
                ui.btn_cb(
                    "⛔️ اخراج (Ban)",
                    f"modact:ban:{chat_id}:{target}",
                ),
                ui.btn_cb("🗑 نادیده گرفتن", "modact:dismiss"),
            ]
        )

    sent = 0
    for user_id in ids:
        try:
            await bot.send_message(
                user_id, text, parse_mode=PARSE_MODE, reply_markup=markup
            )
            sent += 1
        except TelegramRetryAfter as exc:
            await asyncio.sleep(min(float(exc.retry_after), FLOOD_CAP))
            try:
                await bot.send_message(
                    user_id, text, parse_mode=PARSE_MODE, reply_markup=markup
                )
                sent += 1
            except Exception:
                log.debug("mod-log retry undeliverable user=%s", user_id)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            log.debug("mod-log undeliverable user=%s (%s)", user_id, exc)
        except Exception:
            log.warning("mod-log delivery failed user=%s", user_id, exc_info=True)
    log.info(
        "mod-log chat=%s target=%s actor=%s(%s) template=%s delivered=%s/%s",
        chat_id,
        target,
        actor,
        actor_source or "-",
        template,
        sent,
        len(ids),
    )
    return sent


# ------------------------------------------------- Level 2 escalation -----
# An infraction that used to mean "ban" is now a permanent mute applied by the
# bot itself. The bot never removes anybody: the decision goes to the assigned
# Level 2 (Owner) of that chat, alone and in private.
ESCALATION_HEADER = "⚠️ هشدار نیاز به بررسی ادمین سطح ۲ (مالک)"
ESCALATION_CAVEAT = " (رسیدن به سقف اخطار / تخلف بحرانی)"


async def owners(chat_id: int) -> list[int]:
    """Level 2 (Owner) roster only - an escalation never reaches Level 1."""
    try:
        return await rbac.list_admins(chat_id)
    except Exception:
        log.exception("owner roster unavailable chat=%s", chat_id)
        return []


def render_escalation(
    *,
    group_title: str,
    target: int,
    reason: str,
    target_name: str | None = None,
    target_username: str | None = None,
) -> str:
    """The private Level 2 alert (rule 8: HTML-escaped, self-contained)."""
    label = html.escape(target_name) if target_name else str(target)
    lines = [
        ESCALATION_HEADER,
        "",
        f"👥 گروه: {html.escape(str(group_title))}",
        f'👤 کاربر متخلف: <a href="tg://user?id={target}">{label}</a>',
        f"🔗 آیدی تلگرام: {_username_line(target_username)}",
        f"🆔 آیدی عددی: <code>{target}</code>",
        "⚖️ وضعیت فعلی: سکوت دائم توسط ربات",
        f"📝 علت تخلف: {html.escape(reason)}{ESCALATION_CAVEAT}",
        "",
        "📌 تصمیم نهایی: در صورت صلاحدید، می‌توانید با ریپلای دستور «بن» "
        "کاربر را از گروه اخراج کنید.",
    ]
    return "\n".join(lines)


async def deliver_escalation(
    bot: Bot | None,
    chat_id: int,
    *,
    target: int,
    reason: str,
    target_name: str | None = None,
    target_username: str | None = None,
) -> int:
    """Strictly the Level 2 owners; returns how many of them got the alert."""
    bot = _delivery_bot(bot)
    if bot is None:
        return 0
    ids = await owners(chat_id)
    if not ids:
        return 0
    text = render_escalation(
        group_title=await resolve_group_title(bot, chat_id),
        target=target,
        reason=reason,
        target_name=target_name,
        target_username=target_username,
    )
    sent = 0
    for user_id in ids:
        sent += int(await _send(bot, user_id, text))
    log.info(
        "escalation chat=%s target=%s delivered=%s/%s",
        chat_id,
        target,
        sent,
        len(ids),
    )
    return sent


def escalate(
    bot: Bot | None,
    chat_id: int,
    *,
    target: int,
    reason: str,
    target_name: str | None = None,
    target_username: str | None = None,
) -> None:
    """Schedule the Level 2 alert without ever blocking the caller (§7)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.debug("escalation dropped: no running loop (chat=%s)", chat_id)
        return
    task = loop.create_task(
        deliver_escalation(
            bot,
            chat_id,
            target=target,
            reason=reason,
            target_name=target_name,
            target_username=target_username,
        ),
        name=f"escalation:{chat_id}",
    )
    _tasks.add(task)
    task.add_done_callback(_finished)


def _finished(task: asyncio.Task) -> None:
    _tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("mod-log task crashed: %s", exc, exc_info=exc)


def notify(
    bot: Bot | None,
    chat_id: int,
    *,
    action: str,
    target: int,
    actor: int | None = None,
    actor_source: str | None = None,
    reason: str = "",
    roster: list[int] | None = None,
    template: str = "standard",
    target_name: str | None = None,
    target_username: str | None = None,
    message_text: str | None = None,
    strike_count: int | None = None,
    action_taken: str | None = None,
) -> None:
    """Schedule the private fan-out without ever blocking the caller (§7).

    ``bot`` must be the Master bot: every card lands in the PVs of the group's
    Level 2 (Owner) and Level 1 (Sub-Admin) roster, never in the group itself.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.debug("mod-log dropped: no running loop (chat=%s)", chat_id)
        return
    task = loop.create_task(
        deliver(
            bot,
            chat_id,
            action=action,
            target=target,
            actor=actor,
            actor_source=actor_source,
            reason=reason,
            roster=roster,
            template=template,
            target_name=target_name,
            target_username=target_username,
            message_text=message_text,
            strike_count=strike_count,
            action_taken=action_taken,
        ),
        name=f"modlog:{chat_id}",
    )
    _tasks.add(task)
    task.add_done_callback(_finished)
