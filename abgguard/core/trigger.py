from __future__ import annotations

import logging
import re

from abgguard.core import redis as rk
from abgguard.models.entities import DEFAULT_TRIGGER_NAME, Group

log = logging.getLogger("abgguard.trigger")

TRIGGER_MIN = 2
TRIGGER_MAX = 20

_WS = re.compile(r"\s+")


def normalize(text: str | None) -> str:
    """Trim + collapse every run of whitespace (incl. the Persian نیم‌فاصله)."""
    if not text:
        return ""
    return _WS.sub(" ", str(text).replace("\u200c", " ")).strip()


def validate(name: str) -> str:
    """Return the cleaned alias or raise ValueError with a Persian message."""
    cleaned = normalize(name)
    if len(cleaned) < TRIGGER_MIN:
        raise ValueError(f"نام فراخوانی باید حداقل {TRIGGER_MIN} کاراکتر باشد.")
    if len(cleaned) > TRIGGER_MAX:
        raise ValueError(f"نام فراخوانی حداکثر {TRIGGER_MAX} کاراکتر مجاز است.")
    if cleaned.startswith("/"):
        raise ValueError("نام فراخوانی نمی‌تواند با / شروع شود.")
    return cleaned


async def get_trigger(chat_id: int) -> str:
    """Redis-first read; PostgreSQL is the durable fallback.

    Every failure degrades to the default trigger ("پنل") so a broken cache or
    database can never make the panel unreachable.
    """
    try:
        cached = await rk.get_redis().get(rk.k_group_trigger(chat_id))
        if cached:
            return str(cached)
    except Exception:
        log.debug("trigger cache miss chat=%s", chat_id, exc_info=True)

    try:
        rows = await Group.filter(chat_id=chat_id).values_list(
            "trigger_name", flat=True
        )
        name = rows[0] if rows else DEFAULT_TRIGGER_NAME
        try:
            await rk.get_redis().set(rk.k_group_trigger(chat_id), name)
        except Exception:
            log.debug("trigger cache write failed chat=%s", chat_id, exc_info=True)
        return name
    except Exception:
        log.debug("trigger read failed chat=%s", chat_id, exc_info=True)
        return DEFAULT_TRIGGER_NAME


async def set_trigger(chat_id: int, name: str) -> str:
    """Persist the new alias (PostgreSQL first, then the Redis mirror)."""
    cleaned = validate(name)
    row, _ = await Group.get_or_create(chat_id=chat_id, defaults={})
    row.trigger_name = cleaned
    await row.save(update_fields=["trigger_name"])
    await rk.get_redis().set(rk.k_group_trigger(chat_id), cleaned)
    log.info("trigger renamed chat=%s -> %s", chat_id, cleaned)
    return cleaned


async def matches(chat_id: int, text: str | None) -> bool:
    """True when *text* is exactly this group's trigger."""
    cleaned = normalize(text)
    if not cleaned:
        return False
    return cleaned == await get_trigger(chat_id)
