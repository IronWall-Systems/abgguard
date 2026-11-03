"""Periodic SaaS engine - warnings, expiry, panel audit, master heartbeat.

One lightweight asyncio loop (default every 4h, with a 2-min post-boot warm-up
scan) plus a second tiny heartbeat loop (every 15s). Every cycle:

  1. Provision: every roster chat without a subscription row gets a plain
     UNPAID record (so the paywall rollout is visible in the panel).
  2. Warnings: ACTIVE rows crossing <=72h / <=48h / <=24h once per cycle day
     (bitmask `notified_days` = 4 / 2 / 1) - group message + Level 2 DMs.
  3. Expiry: TRIAL / ACTIVE past `expires_at` -> EXPIRED, final Persian notice,
     then BOTH bots leave the chat.
  4. Panel audit: chats the bots still sit in (`abg:presence`) but that have no
     valid DB roster (unbound/deleted by the God Admin) -> both bots leave.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime

from aiogram import Bot

from abgguard.config import settings
from abgguard.core import botpair, rbac
from abgguard.core import groups as groups_store
from abgguard.core import redis as rk
from abgguard.master import ui
from abgguard.models.entities import GroupSubscription
from abgguard.services import subscription

log = logging.getLogger("abgguard.scheduler")

INTERVAL = int(os.getenv("SUB_SCAN_INTERVAL", "14400"))  # 4h audit+scan cycle
INITIAL_DELAY = int(os.getenv("SUB_INITIAL_DELAY", "120"))  # warm-up after boot

_tasks: list[asyncio.Task] = []


def start(bot: Bot) -> None:
    """Spawn the heartbeat + subscription loops (idempotent)."""
    if _tasks:
        return
    _tasks.append(asyncio.create_task(_heartbeat_loop(), name="saas-heartbeat"))
    _tasks.append(asyncio.create_task(_sub_loop(bot), name="saas-scheduler"))
    log.info(
        "SaaS scheduler started (scan every %ss, first sweep in %ss)",
        INTERVAL,
        INITIAL_DELAY,
    )


async def stop() -> None:
    """Cancel both loops cleanly (called from the app's finally block)."""
    tasks = list(_tasks)
    _tasks.clear()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# --------------------------------------------------------------- heartbeat --
async def _heartbeat_loop() -> None:
    """Keep `abg:heartbeat:master` alive: TTL 45s, refresh every 15s."""
    while True:
        try:
            await rk.heartbeat_master()
        except Exception:
            log.warning("heartbeat refresh failed", exc_info=True)
        await asyncio.sleep(settings.heartbeat_interval)


# ------------------------------------------------------------------- loops --
async def _sub_loop(bot: Bot) -> None:
    await asyncio.sleep(INITIAL_DELAY)
    while True:
        try:
            await provision_once()
        except Exception:
            log.exception("subscription provisioning failed")
        try:
            await expiry_and_warnings(bot)
        except Exception:
            log.exception("subscription warnings/expiry scan failed")
        try:
            await audit_once(bot)
        except Exception:
            log.exception("panel audit failed")
        await asyncio.sleep(INTERVAL)


async def provision_once() -> int:
    """UNPAID record for every roster chat that has none (panel visibility)."""
    roster = await groups_store.managed_chat_ids()
    if not roster:
        return 0
    existing = {
        int(c)
        for c in await GroupSubscription.all().values_list("chat_id", flat=True)
    }
    created = 0
    for chat_id in list(roster - existing)[:200]:  # CPU cap: max 200 per cycle
        try:
            titles = await groups_store.known_titles([chat_id])
            await GroupSubscription.create(chat_id=chat_id, title=titles.get(chat_id))
            created += 1
        except Exception:
            log.warning("provision failed chat=%s", chat_id, exc_info=True)
    if created:
        log.info("provisioned %s UNPAID subscription rows", created)
    return created


# ---------------------------------------------------------------- warnings --
async def _warn_bot_message(bot: Bot, user_id: int, chat_id: int, title: str):
    kb = ui.subscribe_kb(chat_id)
    text = (
        "⏳ هشدار اشتراک\n"
        f"🏘 گروه: <b>{title}</b>\n"
        "💳 برای جلوگیری از توقف خدمات همین حالا تمدید کنید."
    )
    try:
        await bot.send_message(user_id, text, reply_markup=kb)
    except Exception:
        log.debug("warning DM failed chat=%s user=%s", chat_id, user_id, exc_info=True)


async def _warn_group(bot: Bot, row: GroupSubscription, days: int) -> None:
    chat_id = row.chat_id
    title = row.title or str(chat_id)
    kb = ui.subscribe_kb(chat_id)
    remaining_text = (
        f"حدود {days} روز" if days > 1 else "کمتر از ۲۴ ساعت"
    )
    text = (
        f"⏳ {ui.icon('clock')} هشدار اشتراک\n"
        f"🏘 گروه: <b>{title}</b>\n"
        f"📅 اعتبار اشتراک ربات {remaining_text} باقی مانده است.\n"
        "💳 برای قطع‌نشدن خدمات، همین حالا تمدید کنید."
    )
    try:
        await bot.send_message(chat_id, text, reply_markup=kb)
    except Exception:
        log.debug("group warning failed chat=%s", chat_id, exc_info=True)
    recipients = {settings.god_admin_id, *await rbac.list_admins(chat_id)}
    for owner in recipients:
        await _warn_bot_message(bot, owner, chat_id, title)


async def expiry_and_warnings(bot: Bot) -> None:
    now = datetime.now(UTC)
    rows = await GroupSubscription.filter(
        status__in=[GroupSubscription.TRIAL, GroupSubscription.ACTIVE]
    )
    for row in rows:
        if row.expires_at is None:
            continue
        remaining = (row.expires_at - now).total_seconds()
        chat_id = row.chat_id

        if remaining <= 0:
            await _expire_chat(bot, row)
            continue

        for days in sorted(settings.warn_days, reverse=True):
            bit = GroupSubscription.notified_bits.get(str(days), 0)
            if remaining <= days * 86_400 and bit and not (row.notified_days & bit):
                await subscription.mark_notified(chat_id, days)
                await _warn_group(bot, row, days)
                break  # one fresh warning per scan crossing


async def _expire_chat(bot: Bot, row: GroupSubscription) -> None:
    """Final pinned notice (step 4), then BOTH bots leave the group."""
    chat_id = row.chat_id
    title = row.title or str(chat_id)
    await subscription.mark_expired(chat_id)
    log.warning("EXPIRED chat=%s (%s) - both bots leaving", chat_id, title)
    await subscription.send_expiry_notice(bot, chat_id, dedupe=False, pin=True)
    try:
        await bot.leave_chat(chat_id)
        log.info("master leave_chat chat=%s", chat_id)
    except Exception:
        log.debug("master leave failed chat=%s", chat_id, exc_info=True)
    await botpair.worker_leave(chat_id)
    await rk.presence_remove(chat_id)


# ------------------------------------------------------------------- audit --
async def audit_once(bot: Bot) -> int:
    """Both bots leave chats they still occupy with no valid panel record."""
    roster = await groups_store.managed_chat_ids()
    evicted = 0
    for chat_id in await rk.presence_known():
        if chat_id in roster:
            continue
        # present for the bots, absent from the panel: revoked / deleted by
        # the God Admin -> immediate clean dual-bot leave
        try:
            await bot.send_message(
                chat_id,
                "🗑 این گروه از پنل مدیریت حذف/لغو شده است؛ ربات‌ها خارج می‌شوند.",
            )
        except Exception:
            pass
        try:
            await bot.leave_chat(chat_id)
            evicted += 1
            log.warning("AUDIT eviction chat=%s - panel record missing", chat_id)
        except Exception:
            log.debug("audit leave failed chat=%s", chat_id, exc_info=True)
        await botpair.worker_leave(chat_id)
        await rk.presence_remove(chat_id)
    return evicted
