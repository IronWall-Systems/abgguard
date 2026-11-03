"""Second-bot (@ABGGUARD1BOT / worker) helpers for the master process.

The master needs an API handle on its sibling for:
  * dual-bot presence checks during onboarding (`get_chat_member`),
  * commanding the worker to leave on expiry / panel audit (`leave_chat`).

Everything is lazily initialised and cached; the getMe call happens once per
process. Failures degrade gracefully - presence checks simply report "not
present" and never raise into an update handler.
"""

from __future__ import annotations

import logging
import time

from aiogram import Bot

from abgguard.config import settings
from abgguard.core.botapi import make_session

log = logging.getLogger("abgguard.botpair")

_admin_rights = "change_info+delete_messages+ban_users+restrict_members+invite_users+pin_messages"
_READY_TTL = 300.0

_worker: Bot | None = None
_worker_id: int | None = None
_worker_name: str | None = None
_ready_cache: dict[int, tuple[float, bool]] = {}


async def init() -> None:
    """Resolve the worker bot's id/username once (getMe only - no conflicts)."""
    global _worker, _worker_id, _worker_name
    if _worker is None:
        _worker = Bot(token=settings.worker_token, session=make_session())
    if _worker_id is None:
        try:
            me = await _worker.me()
            _worker_id = me.id
            _worker_name = me.username or settings.worker_username
        except Exception:
            log.warning("worker bot.me() failed", exc_info=True)
            _worker_name = settings.worker_username


def add_worker_url() -> str:
    """Deep-link that adds @ABGGUARD1BOT to a group with the rights it needs."""
    name = _worker_name or settings.worker_username
    return f"https://t.me/{name}?startgroup&admin={_admin_rights}"


def worker_label() -> str:
    return "@" + (_worker_name or settings.worker_username)


async def worker_present(chat_id: int) -> bool:
    if _worker is None or _worker_id is None:
        return False
    try:
        await _worker.get_chat_member(chat_id, _worker_id)
        return True
    except Exception:
        return False


async def worker_is_admin(chat_id: int) -> bool:
    if _worker is None or _worker_id is None:
        return False
    try:
        member = await _worker.get_chat_member(chat_id, _worker_id)
        return member.status == "administrator"
    except Exception:
        return False


async def worker_guard_ready(chat_id: int) -> bool:
    now = time.monotonic()
    cached = _ready_cache.get(chat_id)
    if cached is not None and now - cached[0] < _READY_TTL:
        return cached[1]
    if _worker is None or _worker_id is None:
        return False
    try:
        member = await _worker.get_chat_member(chat_id, _worker_id)
        ready = str(getattr(member, "status", "")).lower() in {"administrator", "creator"}
    except Exception:
        ready = False
    _ready_cache[chat_id] = (now, ready)
    return ready


async def worker_leave(chat_id: int) -> bool:
    if _worker is None or _worker_id is None:
        return False
    try:
        await _worker.leave_chat(chat_id)
        log.info("worker leave_chat chat=%s", chat_id)
        return True
    except Exception:
        log.warning("worker leave failed chat=%s", chat_id, exc_info=True)
        return False
