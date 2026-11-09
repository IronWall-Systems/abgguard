from __future__ import annotations

import logging
from dataclasses import dataclass

from abgguard.config import settings
from abgguard.core import redis as rk
from abgguard.models.entities import BadWord, GroupPolicy, GroupSettings, PaymentMethod
from abgguard.services.textguards import (
    DEFAULT_FIGHT_WORDS,
    find_fight_word,
)

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
BADWORDS_TTL = 3600  # seconds the Redis snapshot may live before re-reading DB

# Last list logged per chat: keeps the console readable while still printing
# the active list whenever it is (re)loaded or changed.
_BADWORDS_LOGGED: dict[int, frozenset[str]] = {}


def _log_badwords(
    chat_id: int, words: set[str] | list[str] | tuple[str, ...], source: str,
    *, force: bool = False,
) -> None:
    """Print the active blacklist so operators can see what the filter holds."""
    snapshot = frozenset(w.lower() for w in words)
    if not force and _BADWORDS_LOGGED.get(chat_id) == snapshot:
        return
    _BADWORDS_LOGGED[chat_id] = snapshot
    log.info(
        "BADWORDS %s chat=%s count=%d words=[%s]",
        source,
        chat_id,
        len(snapshot),
        ", ".join(sorted(snapshot)),
    )


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
    _log_badwords(chat_id, DEFAULT_FIGHT_WORDS, "seeded", force=True)
    return list(DEFAULT_FIGHT_WORDS)


async def load_badwords(chat_id: int) -> set[str]:
    r = rk.get_redis()
    cached = await r.smembers(rk.k_badwords(chat_id))
    if cached:
        words = {w.lower() for w in cached}
        _log_badwords(chat_id, words, "redis-cache")
        return words

    rows = await BadWord.filter(chat_id=chat_id).values_list("word", flat=True)
    if not rows:
        rows = await _seed_default_badwords(chat_id)
    words = {w.lower() for w in rows}
    if rows:
        pipe = r.pipeline(transaction=False)
        pipe.sadd(rk.k_badwords(chat_id), *rows)
        pipe.expire(rk.k_badwords(chat_id), BADWORDS_TTL)
        await pipe.execute()
    _log_badwords(chat_id, words, "postgres", force=True)
    return words


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
    if added:
        log.info(
            "BADWORDS added chat=%s by=%s words=[%s]",
            chat_id, added_by, ", ".join(words),
        )
    await load_badwords_refresh(chat_id)
    return added


async def remove_badword(chat_id: int, word: str) -> bool:
    deleted = await BadWord.filter(chat_id=chat_id, word=word.strip().lower()).delete()
    if deleted:
        log.info("BADWORDS removed chat=%s word=%s", chat_id, word)
    await load_badwords_refresh(chat_id)
    return bool(deleted)


async def load_badwords_refresh(chat_id: int) -> None:
    """Re-read the authoritative PostgreSQL list and refresh the Redis cache."""
    r = rk.get_redis()
    await r.delete(rk.k_badwords(chat_id))
    rows = await BadWord.filter(chat_id=chat_id).values_list("word", flat=True)
    words = {w.lower() for w in rows}
    if rows:
        pipe = r.pipeline(transaction=False)
        pipe.sadd(rk.k_badwords(chat_id), *rows)
        pipe.expire(rk.k_badwords(chat_id), BADWORDS_TTL)
        await pipe.execute()
    _log_badwords(chat_id, words, "refreshed", force=True)


async def contains_badword(chat_id: int, text: str) -> str | None:
    """Whole-word match against the admin-managed blacklist.

    The list stored in the admin panel (PostgreSQL ``bad_words``, mirrored to
    Redis) is the single source of truth; there is no separate hard-coded
    floor.  ``find_fight_word`` applies strict Persian word boundaries, so an
    entry only fires when it is an independent word (e.g. the harmless
    ``بگیم`` can never match a shorter entry inside it).
    """
    if not text:
        return None
    words = await load_badwords(chat_id)
    hit = find_fight_word(text, words) if words else None
    log.debug(
        "BADWORDS check chat=%s active=%d hit=%s", chat_id, len(words), hit
    )
    return hit


# --------------------------------------------------------------------------
# group policy: CAPTCHA switch + custom welcome template (configured from PV)
# --------------------------------------------------------------------------
@dataclass(slots=True)
class GroupPolicyFlags:
    chat_id: int
    captcha_enabled: bool = False
    auto_approve_enabled: bool = False
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
            auto_approve_enabled=raw.get("auto") == "1",
            welcome_text=raw.get("welcome") or None,
        )

    row = await _ensure_policy(chat_id)
    flags = GroupPolicyFlags(
        chat_id,
        captcha_enabled=row.captcha_enabled,
        auto_approve_enabled=row.auto_approve_enabled,
        welcome_text=row.welcome_text,
    )
    await r.hset(
        rk.k_policy(chat_id),
        mapping={"captcha": "1" if flags.captcha_enabled else "0",
                 "auto": "1" if flags.auto_approve_enabled else "0",
                 "welcome": flags.welcome_text or ""},
    )
    return flags


async def set_captcha(chat_id: int, value: bool) -> None:
    row = await _ensure_policy(chat_id)
    row.captcha_enabled = value
    await row.save(update_fields=["captcha_enabled"])
    await rk.get_redis().hset(rk.k_policy(chat_id), mapping={"captcha": "1" if value else "0"})


async def set_auto_approve(chat_id: int, value: bool) -> None:
    row = await _ensure_policy(chat_id)
    row.auto_approve_enabled = value
    await row.save(update_fields=["auto_approve_enabled"])
    await rk.get_redis().hset(rk.k_policy(chat_id), mapping={"auto": "1" if value else "0"})


async def toggle_policy(chat_id: int, field_name: str) -> GroupPolicyFlags:
    """Flip one policy boolean and return the refreshed flags."""
    if field_name not in {"captcha_enabled", "auto_approve_enabled"}:
        raise KeyError(field_name)
    policy = await get_policy(chat_id)
    new_value = not getattr(policy, field_name)
    if field_name == "captcha_enabled":
        await set_captcha(chat_id, new_value)
    else:
        await set_auto_approve(chat_id, new_value)
    return await get_policy(chat_id)


async def set_welcome(chat_id: int, text: str | None) -> None:
    row = await _ensure_policy(chat_id)
    row.welcome_text = text
    await row.save(update_fields=["welcome_text"])
    await rk.get_redis().hset(rk.k_policy(chat_id), mapping={"welcome": text or ""})


# ---------------------------------------------------------------- payment ---
# God-managed payment gateways (card numbers / USDT wallets). When the table
# is still empty the config env values are served as built-in defaults so the
# payment screen is never blank.
async def payment_list(active_only: bool = True) -> list[PaymentMethod]:
    """Active gateways; falls back to the env-configured defaults."""
    q = PaymentMethod.all()
    if active_only:
        q = q.filter(is_active=True)
    rows = list(await q)
    if rows:
        return rows
    fallback: list[PaymentMethod] = []
    if settings.payment_card_number:
        fallback.append(
            PaymentMethod(
                id=0,
                type=PaymentMethod.CARD,
                title=settings.payment_card_holder[:64] or "کارت بانکی",
                value=settings.payment_card_number,
            )
        )
    if settings.payment_crypto_address:
        fallback.append(
            PaymentMethod(
                id=0,
                type=PaymentMethod.CRYPTO,
                title="Tether TRC20",
                value=settings.payment_crypto_address,
            )
        )
    return fallback


async def payment_add(ptype: str, title: str, value: str) -> PaymentMethod:
    """Create a gateway; only the first non-empty active row matters to users."""
    row = await PaymentMethod.create(type=ptype[:8], title=title[:64], value=value[:128])
    if not row.is_active:  # defensive; created rows are active by default
        row.is_active = True
    return row


async def payment_toggle(pid: int) -> bool:
    """Deactivate/activate a gateway in one click. Returns the new state."""
    row = await PaymentMethod.get_or_none(id=pid)
    if row is None:
        return False
    row.is_active = not row.is_active
    await row.save(update_fields=["is_active"])
    return row.is_active


async def payment_delete(pid: int) -> bool:
    """Remove a gateway permanently (God Admin cleanup)."""
    row = await PaymentMethod.get_or_none(id=pid)
    if row is None:
        return False
    await row.delete()
    return True
