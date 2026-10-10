from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache

from abgguard.core import redis as rk

# Broad emoji / pictograph coverage: pictographs, symbols, supplemental
# arrows, flags, keycaps and the VS16 selector.
EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"   # regional indicators (flags)
    "\U0001F300-\U0001F5FF"   # misc symbols & pictographs
    "\U0001F600-\U0001F64F"   # emoticons
    "\U0001F680-\U0001F6FF"   # transport & map
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"   # supplemental symbols & pictographs
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U00002700-\U000027BF"   # dingbats
    "\U00002600-\U000026FF"   # misc symbols
    "\U00002B00-\U00002BFF"   # arrows & keyboard symbols
    "\U0000FE00-\U0000FE0F"   # variation selectors
    "\U0000200D"              # zero width joiner
    "\U000020E3"              # combining keycap
    "]+"
)

LINK_RE = re.compile(
    r"""(?ix)
    (?:
        https?://\S+
      | \bwww\.[^\s,;:)]+
      | \b t\.me/[^\s,;:)]+
      | \b telegram\.me/[^\s,;:)]+
      | \b(?:[\w-]+\.)+(?:com|net|org|ir|io|me|co|info|xyz|app|dev|ai|shop|store|tv|cc|ru|uk|de|tr|su|ir|biz|club|online|site|store)(?:/[^\s,;:)]*)?
      | \b[\w.+-]+@[\w-]+\.[\w.]{2,}\b
    )
    """
)

_INVITE_RE = re.compile(r"(?i)(?:joinchat/|/\+)[A-Za-z0-9_-]+")


def find_emoji(text: str) -> str | None:
    if not text:
        return None
    m = EMOJI_RE.search(text)
    return m.group(0) if m else None


def find_link(text: str) -> str | None:
    if not text:
        return None
    m = LINK_RE.search(text) or _INVITE_RE.search(text)
    return m.group(0) if m else None


def is_forwarded(message) -> bool:  # aiogram Message
    return getattr(message, "forward_origin", None) is not None


def is_bot_message(message) -> bool:
    """
    Whitelist for the locks: messages authored by a bot (or with no author at
    all) must never be moderated, counted as flood or deleted - the bots must
    never act on each other's messages (rule 1: exactly one of the two bots may
    act on a message, and never on a bot's own output such as the glass panel).
    """
    user = getattr(message, "from_user", None)
    return user is None or bool(getattr(user, "is_bot", False))


async def is_user_vip(chat_id: int, user_id: int) -> bool:
    """Temporary VIP / whitelist status (3 hours).

    While the key lives, the media locks must not touch this member's
    messages - every guard reads the bypass through this one predicate so the
    master and the watchdog can never drift apart.
    """
    return await rk.is_user_vip(chat_id, user_id)


# --------------------------------------------------------------------------
# Strict Fight / Harassment-only filter (Gen-Z friendly by design)
# --------------------------------------------------------------------------
# The shipped blacklist is high-severity fight profanities only.  Mild Gen-Z
# slang (اسکل، دلقک، گوه، کصشعر، تخمی، پشمام) stays legal on purpose and keeps
# flowing into the ML dataset.  Every entry - default or admin added - is run
# through the anti-bypass engine below instead of a naive substring test.

DEFAULT_FIGHT_WORDS: tuple[str, ...] = (
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
)

# Normalisation ------------------------------------------------------------
TATWEEL = "\u0640"  # kashida: مـادرجنده
ZWNJ = "\u200c"  # zero width non joiner: کص‌ننت

# Arabic -> Persian / alef-variant folding, so ک/ي/ى/ة tricks compare equal.
_FOLD = str.maketrans({
    "\u064a": "\u06cc",  # ي -> ی
    "\u0649": "\u06cc",  # ى -> ی
    "\u0643": "\u06a9",  # ك -> ک
    "\u0629": "\u0647",  # ة -> ه
    "\u0623": "\u0627",  # أ -> ا
    "\u0625": "\u0627",  # إ -> ا
    "\u0671": "\u0627",  # ٱ -> ا
})
# Harakat and other combining marks (مَادرجَنده) plus invisible joiners.
_COMBINING = str.maketrans("", "", TATWEEL + ZWNJ + "\u200d\u2060\ufeff\u00ad")
_DIACRITICS = re.compile("[\u064b-\u065f\u0670\u06d6-\u06ed]")
_WS = re.compile(r"\s+")

# Letters that close a word on either side - Persian, Arabic, Latin.
# Digits and glue are deliberately NOT word characters, so "1مادرجنده" is
# still caught while "کتابمادرجنده" (letters glued together) is not a hit.
_LETTERS = r"A-Za-z\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff"

# Glue a cheater may drop between two letters of the same word: spaces, dots,
# digits, punctuation, tatweel leftovers.  The whole word must still be
# spelled in order, so "کتاب صبح کشیدن" can never assemble into کسکش.
_GLUE = (
    r"[\s\d.\-_*·•_,،;؛:!?'\"`´()\[\]{}<>/\\|+=%#&@…"
    r"\u0640\u200c\u200d\u2060\ufeff\u00ad\u00b7]*"
)

_NEVER = re.compile(r"(?!)")

# Persian inflection glued to the word (کسکشی، مادرجنده‌ای، کونی‌ها) or a
# productive prefix (بی‌کسکش).  Boundaries still stop the lookalikes.
_PREFIX = r"(?:بی)"
_INFL = r"(?:ایی|ای|یی|ی|ها|ه|مون|تون|شون|مان|تان|شان|ام|ات|اش)"


def normalize(text: str) -> str:
    """Canonical Persian form: Arabic/Persian letter variants unified, harakat
    + tatweel + zero-width joiners dropped, whitespace collapsed, case folded.

    Applied to BOTH sides of the fight-word comparison, so obfuscation can
    never move a word out of the pattern's reach.
    """
    if not text:
        return ""
    folded = str(text).translate(_FOLD)
    folded = _DIACRITICS.sub("", folded)
    folded = folded.translate(_COMBINING)
    return _WS.sub(" ", folded).strip().casefold()


def _canonical(word: str) -> str:
    """Letters and digits only - separators come back as _GLUE in the pattern."""
    return "".join(ch for ch in normalize(word) if ch.isalnum())


@lru_cache(maxsize=1024)
def _fight_pattern(word: str) -> re.Pattern[str]:
    src = _canonical(word)
    if not src:
        return _NEVER
    body = _GLUE.join(re.escape(ch) for ch in src)
    return re.compile(
        rf"(?:(?<![{_LETTERS}])|{_PREFIX}){body}"
        rf"(?:(?![{_LETTERS}])|{_INFL}+(?![{_LETTERS}]))"
    )


def find_fight_word(text: str, words: Iterable[str]) -> str | None:
    """Return the first blacklist word that hits ``text``, else ``None``.

    Persian word boundaries keep lookalikes legal (کلونی vs کونی, ناموس vs
    بی‌ناموس, کشیدن vs کسکش) while the glue class defeats spaced / dotted /
    ZWNJ / kashida / harakat / digit smuggling.  Suffixes and the بی prefix
    still hit, so کسکشی and بی‌کسکش are not missed.
    """
    if not text or not words:
        return None
    haystack = normalize(text)
    if not haystack:
        return None
    for word in words:
        if word and _fight_pattern(word).search(haystack):
            return word
    return None
