from __future__ import annotations

import logging
from dataclasses import dataclass

from abgguard.core import redis as rk
from abgguard.models.entities import BadWord, GroupPolicy, GroupSettings
from abgguard.services.textguards import DEFAULT_FIGHT_WORDS, find_fight_word

log = logging.getLogger("abgguard.settings")

FIELDS = (
    "porn_lock",
    "photo_lock",
    "video_lock",
    "emoji_lock",
    "gif_lock",
    "link_lock",
    "forward_lock",
)

# Redis hash field names (short -> callback data budget)
RKEY = {
    "porn_lock": "porn",
    "photo_lock": "photo",
    "video_lock": "video",
    "emoji_lock": "emoji",
    "gif_lock": "gif",
    "link_lock": "link",
    "forward_lock": "fwd",
}
INV_RKEY = {v: k for k, v in RKEY.items()}

DEFAULTS: dict[str, bool] = {
    "porn_lock": True,
    "photo_lock": False,
    "video_lock": False,
    "emoji_lock": False,
    "gif_lock": False,
    "link_lock": False,
    "forward_lock": False,
}

BADWORD_CACHE_TTL = 60


@dataclass(slots=True)
class GroupFlags:
    chat_id: int
    porn_lock: bool = True
    photo_lock: bool = False
    video_lock: bool = False
    emoji_lock: bool = False
    gif_lock: bool = False
    link_lock: bool = False
    forward_lock: bool = False

    @classmethod
    def build(cls, chat_id: int, raw: dict[str, str]) -> GroupFlags:
        kwargs: dict[str, bool] = {}
        for field_name in FIELDS:
            key = RKEY[field_name]
            val = raw.get(key)
            kwargs[field_name] = DEFAULTS[field_name] if val is None else val == "1"
        return cls(chat_id=chat_id, **kwargs)

    def as_redis(self) -> dict[str, str]:
        return {RKEY[f]: "1" if getattr(self, f) else "0" for f in FIELDS}

    def is_on(self, field_name: str) -> bool:
        return bool(getattr(self, field_name))

    @property
    def emoji(self) -> str:
        return "✅" if self.porn_lock else "❌"


async def _ensure_row(chat_id: int) -> GroupSettings:
    row, _ = await GroupSettings.get_or_create(
        chat_id=chat_id,
        defaults={f: DEFAULTS[f] for f in FIELDS},
    )
    return row


async def get_flags(chat_id: int) -> GroupFlags:
    """Redis-first read; PostgreSQL is the durable fallback."""
    r = rk.get_redis()
    raw = await r.hgetall(rk.k_settings(chat_id))
    if raw:
        return GroupFlags.build(chat_id, raw)

    row = await _ensure_row(chat_id)
    flags = GroupFlags(
        chat_id=chat_id,
        **{f: getattr(row, f) for f in FIELDS},
    )
    await r.hset(rk.k_settings(chat_id), mapping=flags.as_redis())
    return flags


async def toggle(chat_id: int, field_name: str) -> GroupFlags:
    """Flip one boolean, persist to PostgreSQL and mirror into Redis."""
    if field_name not in FIELDS:
        raise KeyError(field_name)

    flags = await get_flags(chat_id)
    new_value = not flags.is_on(field_name)
    setattr(flags, field_name, new_value)

    row = await _ensure_row(chat_id)
    setattr(row, field_name, new_value)
    await row.save(update_fields=[field_name])

    await rk.get_redis().hset(
        rk.k_settings(chat_id), mapping={RKEY[field_name]: "1" if new_value else "0"}
    )
    return flags


async def set_flag(chat_id: int, field_name: str, value: bool) -> None:
    row = await _ensure_row(chat_id)
    setattr(row, field_name, value)
    await row.save(update_fields=[field_name])
    await rk.get_redis().hset(
        rk.k_settings(chat_id), mapping={RKEY[field_name]: "1" if value else "0"}
    )


# --------------------------------------------------------------------------
# bad words
# --------------------------------------------------------------------------
async def _seed_default_badwords(chat_id: int) -> list[str]:
    """Install the high-severity fight floor for a group.

    Called whenever a group's blacklist is empty - the first time the filter
    is used and again after an admin clears the whole list.  Only severe
    fight/harassment profanities are seeded; mild Gen-Z slang is never part
    of the default set.
    """
    for word in DEFAULT_FIGHT_WORDS:
        await BadWord.get_or_create(
            chat_id=chat_id, word=word, defaults={"added_by": None}
        )
    log.info("badword floor seeded chat=%s words=%d", chat_id, len(DEFAULT_FIGHT_WORDS))
    return list(DEFAULT_FIGHT_WORDS)


async def load_badwords(chat_id: int) -> set[str]:
    r = rk.get_redis()
    cached = await r.smembers(rk.k_badwords(chat_id))
    if cached:
        return {w.lower() for w in cached}

    rows = await BadWord.filter(chat_id=chat_id).values_list("word", flat=True)
    if not rows:
        rows = await _seed_default_badwords(chat_id)
    if rows:
        pipe = r.pipeline(transaction=False)
        pipe.sadd(rk.k_badwords(chat_id), *rows)
        pipe.expire(rk.k_badwords(chat_id), 3600)
        await pipe.execute()
    return {w.lower() for w in rows}


async def add_badwords(chat_id: int, words: list[str], added_by: int) -> int:
    words = [w.strip().lower() for w in words if w and w.strip()]
    if not words:
        return 0
    added = 0
    for word in words:
        _, created = await BadWord.get_or_create(
            chat_id=chat_id, word=word, defaults={"added_by": added_by}
        )
        added += int(created)
    await load_badwords_refresh(chat_id)
    return added


async def remove_badword(chat_id: int, word: str) -> bool:
    deleted = await BadWord.filter(chat_id=chat_id, word=word.strip().lower()).delete()
    await load_badwords_refresh(chat_id)
    return bool(deleted)


async def load_badwords_refresh(chat_id: int) -> None:
    r = rk.get_redis()
    await r.delete(rk.k_badwords(chat_id))
    rows = await BadWord.filter(chat_id=chat_id).values_list("word", flat=True)
    if rows:
        await r.sadd(rk.k_badwords(chat_id), *rows)


async def contains_badword(chat_id: int, text: str) -> str | None:
    """Anti-bypass probe: normalised, word-boundary aware match that returns
    the blacklist entry which fired (for the strike notice)."""
    if not text:
        return None
    words = await load_badwords(chat_id)
    if not words:
        return None
    return find_fight_word(text, words)


# --------------------------------------------------------------------------
# group policy: CAPTCHA switch + custom welcome template (configured from PV)
# --------------------------------------------------------------------------
@dataclass(slots=True)
class GroupPolicyFlags:
    chat_id: int
    captcha_enabled: bool = False
    welcome_text: str | None = None


async def _ensure_policy(chat_id: int) -> GroupPolicy:
    row, _ = await GroupPolicy.get_or_create(chat_id=chat_id, defaults={})
    return row


async def get_policy(chat_id: int) -> GroupPolicyFlags:
    """Redis-first read; PostgreSQL is the durable fallback."""
    r = rk.get_redis()
    raw = await r.hgetall(rk.k_policy(chat_id))
    if raw:
        return GroupPolicyFlags(
            chat_id,
            captcha_enabled=raw.get("captcha") == "1",
            welcome_text=raw.get("welcome") or None,
        )

    row = await _ensure_policy(chat_id)
    flags = GroupPolicyFlags(chat_id, row.captcha_enabled, row.welcome_text)
    await r.hset(
        rk.k_policy(chat_id),
        mapping={"captcha": "1" if flags.captcha_enabled else "0",
                 "welcome": flags.welcome_text or ""},
    )
    return flags


async def set_captcha(chat_id: int, value: bool) -> None:
    row = await _ensure_policy(chat_id)
    row.captcha_enabled = value
    await row.save(update_fields=["captcha_enabled"])
    await rk.get_redis().hset(rk.k_policy(chat_id), mapping={"captcha": "1" if value else "0"})


async def set_welcome(chat_id: int, text: str | None) -> None:
    row = await _ensure_policy(chat_id)
    row.welcome_text = text
    await row.save(update_fields=["welcome_text"])
    await rk.get_redis().hset(rk.k_policy(chat_id), mapping={"welcome": text or ""})
