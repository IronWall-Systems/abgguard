"""SaaS subscription engine - the state and rules behind the paywall.

Everything lives in one PostgreSQL row per group (`GroupSubscription`) with a
short-lived Redis mirror (`abg:sub:{chat_id}`) so the per-message guards can
ask "is this group protected?" at Redis-only cost. Every read fails closed:
a missing/unknown/unpaid row means no protection and no bot features.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from abgguard.config import settings
from abgguard.core import redis as rk
from abgguard.models.entities import GroupSubscription

log = logging.getLogger("abgguard.subscription")

# Pricing constants (called out by the spec)
PRICE_TOMAN = settings.price_toman        # 750000 per 30 days
PRICE_USD = settings.price_usd            # $3 per 30 days
CYCLE_DAYS = settings.cycle_days
TRIAL_HOURS = settings.trial_hours
TRIAL_MIN_MEMBERS = settings.trial_min_members
WARN_DAYS = settings.warn_days            # (3, 2, 1)

PRICE_TEXT = f"{PRICE_TOMAN:,}".replace(",", "٬") + " تومان"

STATUS_LABEL = {
    GroupSubscription.TRIAL: "🟡 تست رایگان",
    GroupSubscription.ACTIVE: "🟢 اشتراک فعال",
    GroupSubscription.MANUAL_WHITELIST: "🟣 ثبت دستی (بدون نیاز به پرداخت)",
    GroupSubscription.EXPIRED: "🔴 منقضی",
    GroupSubscription.UNPAID: "⚪ بدون اشتراک",
}

SUB_CACHE_TTL = 300
DAYS_SECONDS = 86_400
EXPIRY_NOTICE_TTL = 600  # at most one expiry warning every 10 minutes per group

EXPIRY_NOTICE_TEXT = (
    "⏳ مدت استفاده از ربات در این گروه به اتمام رسیده است، لطفا برای استفاده مجدد آن ، "
    "اشتراک را تمدید کنید.\n"
    "⚠️ گروه تحت حفاظت گارد نمی‌باشد."
)
SUPPORT_URL = "https://t.me/pyyple"


def expiry_notice_kb(chat_id: int, *, in_group: bool = True) -> InlineKeyboardMarkup:
    """Renew (deep-link in groups / callback in PV) + direct support."""
    if in_group:
        renew = InlineKeyboardButton(
            text="💳 تمدید و خرید اشتراک",
            url=f"https://t.me/{settings.master_username}?start=pay_{chat_id}",
        )
    else:
        renew = InlineKeyboardButton(
            text="💳 تمدید و خرید اشتراک",
            callback_data=f"sub:pay:{chat_id}",
        )
    support = InlineKeyboardButton(text="💬 پشتیبانی", url=SUPPORT_URL)
    return InlineKeyboardMarkup(inline_keyboard=[[renew], [support]])


async def send_expiry_notice(
    bot: Bot,
    chat_id: int,
    *,
    in_group: bool = True,
    dedupe: bool = True,
    pin: bool = False,
):
    """Send the exact expired-subscription notice, optionally rate-limited.

    Returns the sent ``Message`` (or ``None`` when suppressed/failed). The
    Redis guard is set before the API call so concurrent triggers cannot spam
    the group; it is released again if Telegram rejects the send.
    """
    key = f"{rk.P}:subexpnotice:{chat_id}"
    if dedupe:
        try:
            if not await rk.get_redis().set(key, "1", ex=EXPIRY_NOTICE_TTL, nx=True):
                return None
        except Exception:
            log.debug("expiry notice guard unavailable chat=%s", chat_id, exc_info=True)
    try:
        sent = await bot.send_message(
            chat_id,
            EXPIRY_NOTICE_TEXT,
            reply_markup=expiry_notice_kb(chat_id, in_group=in_group),
        )
    except Exception:
        log.debug("expiry notice failed chat=%s", chat_id, exc_info=True)
        if dedupe:
            try:
                await rk.get_redis().delete(key)
            except Exception:
                pass
        return None
    if pin:
        try:
            await bot.pin_chat_message(
                chat_id=chat_id,
                message_id=sent.message_id,
                disable_notification=False,
            )
        except Exception:
            log.debug("expiry notice pin failed chat=%s", chat_id, exc_info=True)
    return sent


# ----------------------------------------------------------------- reads ----
async def _row(chat_id: int) -> GroupSubscription | None:
    try:
        return await GroupSubscription.get_or_none(chat_id=chat_id)
    except Exception:
        log.warning("subscription read failed chat=%s", chat_id, exc_info=True)
        return None


async def _cache_now(chat_id: int, row: GroupSubscription) -> None:
    exp_ts = int(row.expires_at.timestamp()) if row.expires_at else 0
    try:
        await rk.get_redis().hset(
            rk.k_sub_state(chat_id),
            mapping={"status": row.status, "expires": str(exp_ts)},
        )
        await rk.get_redis().expire(rk.k_sub_state(chat_id), SUB_CACHE_TTL)
    except Exception:
        pass


async def get_record(chat_id: int) -> GroupSubscription:
    """Always returns a row (UNPAID when none exists yet)."""
    row = await _row(chat_id)
    if row is None:
        return GroupSubscription(chat_id=chat_id, status=GroupSubscription.UNPAID)
    return row


def _row_is_active(row: GroupSubscription) -> bool:
    now = datetime.now(UTC)
    if row.status == GroupSubscription.MANUAL_WHITELIST:
        return True
    if row.status == GroupSubscription.ACTIVE:
        return row.expires_at is not None and row.expires_at > now
    if row.status == GroupSubscription.TRIAL:
        return row.expires_at is not None and row.expires_at > now
    return False


async def _cached_status(chat_id: int) -> tuple[str, datetime | None] | None:
    try:
        raw = await rk.get_redis().hgetall(rk.k_sub_state(chat_id))
    except Exception:
        return None
    if not raw:
        return None
    exp_ts = int(raw.get("expires") or 0)
    expires = datetime.fromtimestamp(exp_ts, UTC) if exp_ts else None
    return raw.get("status", ""), expires


def _status_active(status: str, expires: datetime | None) -> bool:
    """Fail-closed: only MANUAL_WHITELIST, unexpired ACTIVE or TRIAL pass."""
    if status == GroupSubscription.MANUAL_WHITELIST:
        return True
    if status in (GroupSubscription.ACTIVE, GroupSubscription.TRIAL):
        return expires is not None and expires > datetime.now(UTC)
    return False


async def is_active(chat_id: int) -> bool:
    """The bot may work only when the group has an explicitly live plan."""
    cached = await _cached_status(chat_id)
    if cached is not None:
        return _status_active(*cached)
    row = await _row(chat_id)
    if row is None:
        return False
    await _cache_now(chat_id, row)
    return _row_is_active(row)


async def is_protected(chat_id: int) -> bool:
    """Guard gate - identical fail-closed rule: no row/unpaid/expired = False."""
    return await is_active(chat_id)


def days_left(row: GroupSubscription) -> float | None:
    if row.expires_at is None:
        return None
    remaining = (row.expires_at - datetime.now(UTC)).total_seconds()
    return remaining / DAYS_SECONDS


# ---------------------------------------------------------------- writes ----
async def _invalidate(chat_id: int) -> None:
    try:
        await rk.get_redis().delete(rk.k_sub_state(chat_id))
    except Exception:
        pass


async def _sync_registry(chat_id: int, title: str | None) -> None:
    """Keep Group + RoleBinding aligned with every subscription row."""
    try:
        from abgguard.core import groups as groups_store

        await groups_store.upsert_group(chat_id, title=title)
        await groups_store.ensure_owner(chat_id)
    except Exception:
        log.debug("group registry sync failed chat=%s", chat_id, exc_info=True)


async def record_join(chat_id: int, title: str | None = None) -> GroupSubscription:
    """Create-or-refresh the row when the master bot is (re-)added to a group."""
    row = await _row(chat_id)
    if row is not None:
        if title and title != row.title:
            row.title = title
            try:
                await row.save(update_fields=["title"])
            except Exception:
                log.warning("subscription title update failed", exc_info=True)
        await _sync_registry(chat_id, title or row.title)
        return row
    try:
        row = await GroupSubscription.create(chat_id=chat_id, title=title)
    except Exception:
        row = await _row(chat_id)
        log.warning("subscription create failed chat=%s", chat_id, exc_info=True)
        row = row if row else GroupSubscription(chat_id=chat_id)
    await _sync_registry(chat_id, title or row.title)
    return row


async def _save_status(
    row: GroupSubscription,
    status: str,
    expires_at: datetime | None,
    *,
    reset_warn: bool = True,
) -> GroupSubscription:
    row.status = status
    row.expires_at = expires_at
    if reset_warn:
        row.notified_days = 0
    await row.save(update_fields=["status", "expires_at", "notified_days"])
    await _invalidate(row.chat_id)
    return row


async def activate_trial(chat_id: int) -> GroupSubscription:
    """24-hour free trial - `trial_used` is set on the group permanently."""
    expires = datetime.now(UTC) + timedelta(hours=TRIAL_HOURS)
    row = await record_join(chat_id)
    if not row.trial_used:
        row.trial_used = True
        await row.save(update_fields=["trial_used"])
    return await _save_status(row, GroupSubscription.TRIAL, expires)


async def activate_paid(chat_id: int, days: int = CYCLE_DAYS) -> GroupSubscription:
    """One 30-day cycle: stacks on top of a still-active cycle, restarts otherwise."""
    row = await record_join(chat_id)
    now = datetime.now(UTC)
    if row.status == GroupSubscription.ACTIVE and row.expires_at and row.expires_at > now:
        base = row.expires_at
    else:
        base = now
    return await _save_status(row, GroupSubscription.ACTIVE, base + timedelta(days=days))


async def manual_whitelist(chat_id: int, title: str | None = None) -> GroupSubscription:
    """God Admin override - never asked for payment, never expires."""
    row = await record_join(chat_id, title)
    return await _save_status(row, GroupSubscription.MANUAL_WHITELIST, None)


async def mark_expired(chat_id: int) -> None:
    row = await _row(chat_id)
    if row is None or row.status in (
        GroupSubscription.EXPIRED,
        GroupSubscription.MANUAL_WHITELIST,
        GroupSubscription.UNPAID,
    ):
        return
    await _save_status(row, GroupSubscription.EXPIRED, row.expires_at)


async def mark_notified(chat_id: int, days: str | int) -> None:
    """Record that the N-day warning was sent for the current cycle."""
    bit = GroupSubscription.notified_bits.get(str(days))
    if bit is None:
        return
    row = await _row(chat_id)
    if row is None or row.notified_days & bit:
        return
    row.notified_days |= bit
    await row.save(update_fields=["notified_days"])
    await _invalidate(chat_id)
