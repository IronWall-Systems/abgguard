from __future__ import annotations

import enum
import hashlib
import logging
import time

from abgguard.config import settings
from abgguard.core import redis as rk

log = logging.getLogger("abgguard.flood")

REP_WINDOW = 10  # seconds for the "same text" bucket
REP_LIMIT = 3    # identical messages inside REP_WINDOW == flood


class FloodKind(enum.Enum):
    NONE = "none"
    TEXT_RATE = "text_rate"
    TEXT_REPEAT = "text_repeat"
    JOIN = "join"

    @property
    def fa(self) -> str:
        return {
            FloodKind.TEXT_RATE: "ارسال پیام‌های متوالی سریع (فلود متن)",
            FloodKind.TEXT_REPEAT: "ارسال پیام‌های تکراری متوالی (اسپم)",
            FloodKind.JOIN: "موج عضویت گروه (فلود عضویت)",
            FloodKind.NONE: "-",
        }[self]


def _bucket(member: str) -> str:
    return hashlib.blake2s(member.encode(), digest_size=8).hexdigest()


async def _window_count(key: str, window: int, member: str) -> int:
    """Sliding window counter backed by a Redis sorted-set."""
    r = rk.get_redis()
    now = time.time()
    pipe = r.pipeline(transaction=False)
    pipe.zremrangebyscore(key, 0, now - window)
    pipe.zadd(key, {member: now})
    pipe.zcard(key)
    pipe.expire(key, window + 2)
    results = await pipe.execute()
    return int(results[2])


async def check_text_flood(chat_id: int, user_id: int, text: str, message_id: int) -> FloodKind:
    """5 messages / 3s  OR  3 identical messages / 10s  ->  flood."""
    count = await _window_count(
        rk.k_flood_text(chat_id, user_id),
        settings.flood_text_window,
        str(message_id),
    )
    if count >= settings.flood_text_limit:
        return FloodKind.TEXT_RATE

    clean = " ".join((text or "").split()).lower()
    if clean:
        rep_key = f"{rk.FLOOD_TEXT}:{chat_id}:{user_id}:rep:{_bucket(clean)}"
        rep_count = await _window_count(rep_key, REP_WINDOW, str(message_id))
        if rep_count >= REP_LIMIT:
            return FloodKind.TEXT_REPEAT

    return FloodKind.NONE


async def check_join_flood(chat_id: int, user_id: int, join_id: str) -> FloodKind:
    """Mass-join detection: strictly more than FLOOD_JOIN_LIMIT joins / 60s."""
    count = await _window_count(
        rk.k_flood_join(chat_id),
        settings.flood_join_window,
        join_id,
    )
    if count > settings.flood_join_limit:
        return FloodKind.JOIN
    return FloodKind.NONE


async def is_join_locked(chat_id: int) -> bool:
    return bool(await rk.get_redis().exists(rk.k_join_lock(chat_id)))


async def set_join_lock(chat_id: int, ttl: int) -> None:
    await rk.get_redis().set(rk.k_join_lock(chat_id), "1", ex=ttl)


async def clear_join_lock(chat_id: int) -> None:
    await rk.get_redis().delete(rk.k_join_lock(chat_id))


async def purge(chat_id: int, user_id: int) -> None:
    r = rk.get_redis()
    keys = [rk.k_flood_text(chat_id, user_id), rk.k_flood_hash(chat_id, user_id)]
    if keys:
        await r.delete(*keys)
