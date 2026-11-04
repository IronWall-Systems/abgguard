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


# --------------------------------------------------------------------------
# Advanced anti-obfuscation floor
# --------------------------------------------------------------------------
# A second, deliberately aggressive pass.  It is independent of the admin
# blacklist: the roots below are always active, so spaced / dotted / ZWNJ /
# repeated-character smuggling can never hide them.
BAD_WORDS_ROOTS: set[str] = {
    # Base Organs & Slang
    "کیر", "کون", "خایه", "شاش", "گوه", "گه", "چوچول", "جق",
    "کیرم", "کیرت", "کیرش", "کیری", "کسش", "کصش",
    "کونم", "کونت", "کونش", "خایم", "خایت", "خایش",

    # Direct Insults & Slurs
    "جنده", "قحبه", "قهبه", "کسکش", "کصکش", "کونی", "دیوث", "دایوس", "پفیوز", "لاشی",
    "حرومزاده", "حرامزاده", "ابنه", "اوبی", "سلیطه", "پتیاره", "لکاته", "بیناموس", "تخمسگ",
    "پدرسگ", "توله‌سگ", "تولهسگ", "کفتار", "شاشو",

    # Family Combinations (Mother, Sister, Aunt, Wife, etc.)
    "مادرجنده", "مادرسگ", "مادرکسه", "مادرکصده", "مادرکسده", "مادرخراب", "مادرقحبه", "مادرقهبه", "مادرکص",
    "ننهجنده", "ننهکسه", "ننهکصده", "ننهخراب", "ننهقحبه", "ننهکس",
    "خواهرجنده", "خواهرکسه", "خواهرکصده", "خواهرخراب", "خواهرکس",
    "خارکسه", "خارکصده", "خارجنده", "خارخراب", "خارکس",
    "خوارکسه", "خوارکصده", "خوارجنده", "خوارخراب", "خوارقحبه", "خوارکس",
    "عمهجنده", "عمهکسه", "عمهخراب", "عمهکصده",
    "زنجنده", "زنقحبه", "زنسلیطه",

    # Cognitive / Compound Insults
    "کسخل", "کصخل", "کسمغز", "کصمغز", "کسشر", "کصشر", "کصشعر", "کسشعر", "کسده", "کصده",
    "کسلیس", "کصلیس", "کیرلیس", "کونلیس", "خایمال", "خایهمال", "گهخور", "گوهخور", "شاشخور",
    "کسپاره", "کصپاره", "کونپاره",

    # Action / Vulgar Verbs
    "گاییدم", "گایید", "میگام", "میگایم", "بگا", "بگام", "بگیم", "میگامت", "گاییده",
    "ساکزن", "جقی", "جقزن", "حشری", "سکس", "پورن",
    "تلمبه", "صیغه", "کیرمتو", "بیاخور",

    # Variations & Typo Catchers
    "ک30", "ک3", "کصکشا", "کونیا", "جندها", "کیریا", "لاشیها", "لاشیا",
}

# Exact severe phrases - the regex builder's \W* handles any spaces/dots
# between letters, so "کیرم تو کس" also matches "کیرم.تو.کس" etc.
BAD_PHRASES: set[str] = {
    "کیرم تو کس", "کیرم تو کص", "کیرم دهنت", "کیرم تو دهنت", "کیرم تو حلق",
    "کس مادرت", "کص مادرت", "کس ننت", "کص ننت", "کس خارت", "کص خارت",
    "کس خواهرت", "تخم سگ", "تخم جن", "تخم حروم", "ساک بزن", "بیا بخور",
}

# Proximity matching: catch ambiguous words ONLY when they appear near
# target words (e.g., "کس" within 15 chars of "مادر"), handling all
# spacing/noise in between via .{0,15}.
CONTEXT_PATTERNS: list[str] = [
    r"(کس|کص|قس|ممه).{0,15}(مادر|ننه|خار|خواهر|خوار|عمه|زن|عشقت)",
    r"(مادر|ننه|خار|خواهر|خوار|عمه|زن|عشقت).{0,15}(کس|کص|قس|جنده|خراب|سگ|قهبه)",
    r"(تخم).{0,15}(سگ|جن|حروم|حرام|سایه)",
    r"(کیرم|کیر).{0,15}(دهنت|کس|کص|کون|حلق|تو|مادر|ننه|خار|خواهر)",
]
COMPILED_CONTEXT: list[re.Pattern] = [re.compile(p, re.IGNORECASE) for p in CONTEXT_PATTERNS]

# Arabic -> Persian + alef variants for the aggressive pass.
_ADV_FOLD = str.maketrans({
    "\u064a": "\u06cc",  # ي -> ی
    "\u0649": "\u06cc",  # ى -> ی
    "\u0643": "\u06a9",  # ك -> ک
    "\u0629": "\u0647",  # ة -> ه
    "\u0623": "\u0627",  # أ -> ا
    "\u0625": "\u0627",  # إ -> ا
    "\u0671": "\u0627",  # ٱ -> ا
    "\u0624": "\u0648",  # ؤ -> و
})

# Every separator / symbol an offender can inject between letters.
_ADV_NOISE = re.compile(
    r"[\s\u200c\u200d\u2060\ufeff\u00ad\u0640"
    r".\-_*,،;؛:!?؟'\"`´()\[\]{}<>/\\|+=~@#%&…·•]+"
)
_REPEATS = re.compile(r"(.)\1+")


def normalize_text(text: str) -> str:
    """Aggressive canonical form for the advanced floor.

    Arabic letters are folded to Persian, diacritics / tatweel / zero-width
    characters are dropped, every space / dot / dash / punctuation noise is
    removed, and runs of one character collapse to a single one.  ``ک ی ر``,
    ``ک.ی.ر``, ``ک‌ون`` and ``کوووون`` therefore all become ``کیر`` / ``کون``.
    """
    if not text:
        return ""
    folded = str(text).translate(_ADV_FOLD)
    folded = _DIACRITICS.sub("", folded)
    folded = folded.translate(_COMBINING)
    folded = _ADV_NOISE.sub("", folded)
    folded = _REPEATS.sub(r"\1", folded)
    return folded.casefold()


# Words that are also ordinary Persian (کس = person, تخم = egg, کردن = to do,
# دادن = to give, ...).  They only fire when the whole message is the word
# itself; otherwise a normal sentence would be muted.  Any real compound
# (کسکش، تخمسگ، کیر کردن) is still caught by its own explicit root.
_CONTEXT_REQUIRED = frozenset({
    "کس", "قس", "گه", "تخم", "ساک", "بکن", "کردن", "کردم", "دادن", "دادم",
    "میدم", "تلمبه", "حامله", "صیغه",
})

def _build_profanity_regex(bad_words: set[str]) -> re.Pattern:
    """Build a single compiled regex that matches any bad word with word
    boundaries, allowing optional non-word separators and repeated chars
    between letters.  Keeps CPU/RAM minimal - one pass over the text."""
    patterns = []
    for word in bad_words:
        # Ignore spaces inside phrases: the separator between letters is
        # already \W*, so "کص ننت" must also match "کص.ننت" and "کص  ننت".
        letters = [char for char in word if not char.isspace()]
        if not letters:
            continue
        # For each letter, require at least one occurrence (+),
        # separated by optional non-word characters (\W*) like spaces, dots, ZWNJ.
        pattern_body = r"\W*".join(re.escape(char) + r"+" for char in letters)
        # Lookbehind/lookahead enforce word boundaries so ``عکس`` does not
        # match ``کس`` and ``تخمه`` does not match ``تخم``.
        patterns.append(rf"(?<!\w){pattern_body}(?!\w)")

    combined = "|".join(patterns)
    return re.compile(combined, re.IGNORECASE)


PROFANITY_REGEX = _build_profanity_regex(BAD_WORDS_ROOTS | BAD_PHRASES)


def find_badword_root(text: str) -> str | None:
    r"""Return the matched bad-word substring that fires on ``text``, else
    ``None``.

    Uses a compiled regex with word boundaries so innocent words like
    ``عکس`` (picture) and ``تخمه`` (seed) do not trigger ``کس`` / ``تخم``.
    Spaces and punctuation are kept so the ``\w`` / ``\W`` boundaries work.
    Checks single-word roots and exact severe phrases via PROFANITY_REGEX,
    then falls back to contextual proximity patterns (COMPILED_CONTEXT) so
    ambiguous words only fire near target words.
    """
    if not text:
        return None

    # Fold characters and remove diacritics/tatweel, but KEEP spaces/punctuation
    # so the Regex word boundaries (\w and \W) function correctly.
    folded = str(text).translate(_ADV_FOLD)
    folded = _DIACRITICS.sub("", folded)
    folded = folded.translate(_COMBINING)
    folded = folded.replace("\u200c", " ").replace("ـ", "")
    folded = folded.casefold()

    # 1. Single-word roots and exact severe phrases via the compiled regex
    match = PROFANITY_REGEX.search(folded)
    if match:
        return match.group(0)

    # 2. Contextual proximity - ambiguous words only near target words
    for pattern in COMPILED_CONTEXT:
        match = pattern.search(folded)
        if match:
            return match.group(0)

    return None


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
