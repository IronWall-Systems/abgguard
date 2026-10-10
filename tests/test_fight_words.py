from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import ClassVar

import pytest
from aiogram import Bot
from aiogram.types import Chat, Message, Update, User

from abgguard.core import rbac, settings_store
from abgguard.core import redis as rk
from abgguard.core.rbac import Role
from abgguard.core.settings_store import GroupFlags
from abgguard.master.handlers import guards
from abgguard.services import antiflood, strikes
from abgguard.services.textguards import DEFAULT_FIGHT_WORDS, find_fight_word

# mild Gen-Z slang - must never be blocked and must keep flowing into the ML
# dataset.  Nothing here belongs in the default blacklist.
SLANG = ("اسکل", "دلقک", "گوه", "عن", "کصشعر", "تخمی", "پشمام")

_UPDATE_ID = 900_000


def _uid() -> int:
    global _UPDATE_ID
    _UPDATE_ID += 1
    return _UPDATE_ID


# ------------------------------------------------------------------ engine ---
def test_default_blacklist_is_severe_fight_words_only() -> None:
    assert set(DEFAULT_FIGHT_WORDS) == {
        "مادرجنده",
        "کص‌ننت",
        "خارکصه",
        "کسکش",
        "کصکش",
        "کونی",
        "مادرقحبه",
        "بی‌ناموس",
        "خواهرکصه",
        "حرومزاده",
        "مادرخراب",
    }
    assert not set(SLANG) & set(DEFAULT_FIGHT_WORDS)


def test_mild_gslang_is_never_a_hit() -> None:
    text = "تو فقط یه اسکل و دلقک و گوه و کصشعر و تخمی و پشمامی"
    assert find_fight_word(text, DEFAULT_FIGHT_WORDS) is None


@pytest.mark.parametrize(
    "variant",
    [
        "مادرجنده",
        "م ا د ر ج ن د ه",  # letter spaced
        "مادر.جنده",  # dotted
        "مادر‌جنده",  # zero width non joiner
        "مـادرجنده",  # kashida
        "مَادرجَنده",  # harakat
        "مادر1جنده",  # digit glue
        "مادر-جنده",
        "ای مادرجنده",
        "کسکش",
        "كسكش",  # arabic kaf
        "کسکشی",  # inflection
        "بی‌کسکش",  # prefix
        "کصننت",
        "ک ص ن ن ت",
        "کص.ننت",
        "بیناموس",
        "بی ناموس",
        "مادرجنده‌ای",
        "کونی‌ها",
    ],
)
def test_every_bypass_variant_still_hits(variant: str) -> None:
    assert find_fight_word(variant, set(DEFAULT_FIGHT_WORDS)) is not None


@pytest.mark.parametrize(
    "text",
    [
        "کلونی گربه‌ها را دیدم",  # کونی lookalike
        "ناموس داریم",  # بی‌ناموس lookalike
        "کشیدن سیگار ممنوع",  # کسکش lookalike
        "مادرانه رفتار کن",  # مادرجنده lookalike
        "خارپستی دریایی",  # خارکصه lookalike
        "کتاب عنوان جالبی دارد",
        "عن مقاله",  # عن is not a fight word
    ],
)
def test_legit_persian_words_stay_legal(text: str) -> None:
    assert find_fight_word(text, set(DEFAULT_FIGHT_WORDS)) is None


def test_an_admin_word_only_fires_on_its_own_boundaries() -> None:
    words = {"عن"}
    assert find_fight_word("کتاب عنوان", words) is None
    assert find_fight_word("امروز عن کرد", words) == "عن"


# --------------------------------------------------------------- the floor ---
class _Pipe:
    def __init__(self) -> None:
        self.ops: list[tuple] = []

    def sadd(self, key: str, *values: str) -> _Pipe:
        self.ops.append(("sadd", key, values))
        return self

    def expire(self, key: str, ttl: int) -> _Pipe:
        self.ops.append(("expire", key, ttl))
        return self

    async def execute(self) -> list[bool]:
        return [True] * len(self.ops)


class _FakeRedis:
    async def smembers(self, key: str) -> set[str]:
        return set()

    def pipeline(self, transaction: bool = False) -> _Pipe:
        return _Pipe()


class _Query:
    def __init__(self, rows: set[str]) -> None:
        self._rows = rows

    async def values_list(self, *args, **kwargs) -> list[str]:
        return list(self._rows)


class _BadWordStub:
    rows: ClassVar[dict[int, set[str]]] = {}

    @classmethod
    def filter(cls, chat_id: int) -> _Query:
        return _Query(cls.rows.get(chat_id, set()))

    @classmethod
    async def get_or_create(cls, *, chat_id: int, word: str, defaults=None):
        bucket = cls.rows.setdefault(chat_id, set())
        created = word not in bucket
        bucket.add(word)
        return None, created


def _install_store(monkeypatch) -> None:
    _BadWordStub.rows = {}
    monkeypatch.setattr(rk, "get_redis", lambda: _FakeRedis())
    monkeypatch.setattr(settings_store, "BadWord", _BadWordStub)


def test_first_use_seeds_only_the_severe_floor(monkeypatch) -> None:
    _install_store(monkeypatch)

    words = asyncio.run(settings_store.load_badwords(-1001))

    assert words == set(DEFAULT_FIGHT_WORDS)
    assert _BadWordStub.rows[-1001] == set(DEFAULT_FIGHT_WORDS)
    assert not set(SLANG) & words


def test_clearing_the_whole_list_restores_the_floor(monkeypatch) -> None:
    _install_store(monkeypatch)
    asyncio.run(settings_store.load_badwords(-1001))
    _BadWordStub.rows[-1001] = set()  # an admin used پاک کردن همه

    words = asyncio.run(settings_store.load_badwords(-1001))

    assert words == set(DEFAULT_FIGHT_WORDS)


def test_admin_words_are_not_reseeded_away(monkeypatch) -> None:
    _install_store(monkeypatch)
    _BadWordStub.rows[-1001] = {"اسکل"}  # a trimmed, admin owned list

    words = asyncio.run(settings_store.load_badwords(-1001))

    assert words == {"اسکل"}


def test_contains_badword_returns_the_entry_that_fired(monkeypatch) -> None:
    async def _words(chat_id: int) -> set[str]:
        return set(DEFAULT_FIGHT_WORDS)

    monkeypatch.setattr(settings_store, "load_badwords", _words)

    assert asyncio.run(settings_store.contains_badword(-1, "این کسکشه")) == "کسکش"
    assert asyncio.run(settings_store.contains_badword(-1, "کسکش")) == "کسکش"
    assert asyncio.run(settings_store.contains_badword(-1, "کلونی و ناموس")) is None
    assert asyncio.run(settings_store.contains_badword(-1, "اسکل و دلقک")) is None
    assert asyncio.run(settings_store.contains_badword(-1, "")) is None


# ----------------------------------------------------------- group pipeline ---
BOT_USER = User(id=1000, is_bot=True, first_name="ABG Guard", username="abg_guard_bot")


class _Rec:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.role: Role = Role.USER
        self.deleted: list[tuple[int, int]] = []
        self.dirty: list[tuple[int, int]] = []
        self.strikes: list[tuple[int, int]] = []
        self.mutes: list[tuple[int, int, int]] = []
        self.bans: list[tuple[int, int, str]] = []
        self.harvested: list[str] = []
        self.cached: list[int] = []
        self.sent: list[str] = []
        self.sources: list[str | None] = []


REC = _Rec()


def _message(text: str, *, chat_id: int = -1001, uid: int = 7, message_id: int = 5) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type="supergroup", title="گروه تست"),
        from_user=User(id=uid, is_bot=False, first_name="علی"),
        text=text,
    )


async def _get_role(chat_id: int, user_id: int) -> Role:
    return REC.role


async def _get_flags(chat_id: int) -> GroupFlags:
    return GroupFlags(chat_id=chat_id, porn_lock=False)


async def _flood(*args, **kwargs):
    return antiflood.FloodKind.NONE


async def _words(chat_id: int) -> set[str]:
    return set(DEFAULT_FIGHT_WORDS)


async def _cache_message(chat_id: int, message_id: int, user_id: int, text: str) -> None:
    REC.cached.append(message_id)


async def _mark_dirty(chat_id: int, message_id: int) -> None:
    REC.dirty.append((chat_id, message_id))


async def _delete(bot, chat_id: int, message_id: int) -> bool:
    REC.deleted.append((chat_id, message_id))
    return True


async def _mute(
    bot,
    *,
    chat_id: int,
    user_id: int,
    message_id,
    seconds: int,
    reason: str,
    actor_source: str | None = None,
):
    REC.mutes.append((chat_id, user_id, seconds))
    REC.sources.append(actor_source)
    return True


async def _ban(
    bot,
    *,
    chat_id: int,
    user_id: int,
    message_id,
    reason: str,
    notify=None,
    actor_source: str | None = None,
):
    REC.bans.append((chat_id, user_id, reason))
    REC.sources.append(actor_source)
    return True


async def _register_strike(chat_id: int, user_id: int) -> int:
    REC.strikes.append((chat_id, user_id))
    return 1


async def _harvest(bot, message: Message) -> None:
    REC.harvested.append(message.text or "")


async def _bot_send_message(self, chat_id, text=None, *args, **kwargs) -> None:
    REC.sent.append(str(text))
    return None


def _install(monkeypatch) -> None:
    REC.reset()
    monkeypatch.setattr(rbac, "get_role", _get_role)
    monkeypatch.setattr(settings_store, "get_flags", _get_flags)
    monkeypatch.setattr(settings_store, "load_badwords", _words)
    monkeypatch.setattr(antiflood, "check_text_flood", _flood)
    monkeypatch.setattr(strikes, "register", _register_strike)
    monkeypatch.setattr(guards, "_cache_message", _cache_message)
    monkeypatch.setattr(guards, "_mark_dirty", _mark_dirty)
    monkeypatch.setattr(guards, "safe_delete", _delete)
    monkeypatch.setattr(guards, "delete_and_mute", _mute)
    monkeypatch.setattr(guards, "delete_and_ban", _ban)
    monkeypatch.setattr(guards, "_harvest", _harvest)
    monkeypatch.setattr(Bot, "send_message", _bot_send_message)


def _feed(master, text: str) -> None:
    bot, dp = master
    msg = _message(text)
    asyncio.run(dp.feed_update(bot=bot, update=Update(update_id=_uid(), message=msg)))


def test_severe_word_is_deleted_struck_and_kept_out_of_the_dataset(master, monkeypatch) -> None:
    _install(monkeypatch)
    _feed(master, "تو یه مادرجنده‌ای هستی")

    assert REC.deleted == [(-1001, 5)]
    assert REC.dirty == [(-1001, 5)], "the deleted message must be flagged dirty"
    assert REC.strikes == [(-1001, 7)], "the offense counter must increment"
    assert REC.mutes == [(-1001, 7, 600)]
    assert REC.bans == []
    assert REC.harvested == [], "a fight word must never reach the ML dataset"
    assert REC.sources == ["master"], "an automatic guard acts as the Master bot"


def test_obfuscated_severe_word_is_still_caught(master, monkeypatch) -> None:
    _install(monkeypatch)
    _feed(master, "کص.ننت")

    assert REC.deleted == [(-1001, 5)]
    assert REC.dirty == [(-1001, 5)]
    assert REC.harvested == []


def test_mild_gslang_passes_and_reaches_the_dataset(master, monkeypatch) -> None:
    _install(monkeypatch)
    _feed(master, "اسکل و دلقک و گوه و پشمام")

    assert REC.deleted == []
    assert REC.dirty == []
    assert REC.strikes == []
    assert REC.mutes == []
    assert REC.harvested == ["اسکل و دلقک و گوه و پشمام"]
