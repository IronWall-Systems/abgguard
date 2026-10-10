from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from abgguard.config import settings
from abgguard.core.rbac import Role
from abgguard.core.settings_store import RKEY, GroupFlags, GroupPolicyFlags
from abgguard.models.entities import DEFAULT_TRIGGER_NAME

# ---------------------------------------------------------------------------
# Visual "colour" protocol (spec section 1).
# Telegram inline keyboards have no raw CSS hex backgrounds, so the palette is
# expressed through status glyphs + styled text badges.
# ---------------------------------------------------------------------------
GREEN = "🟢"     # active / enabled / healthy
RED = "🔴"       # disabled / locked / danger
BLUE = "🔵"      # navigation / action
YELLOW = "🟡"    # logs / pending / warnings
PURPLE = "🟣"    # AI / ML harvester & NudeNet
NEUTRAL = "⚪"   # selectors / neutral

BACK = "🔙"
CLOSE = "❌"

# callback_data budget is 64 bytes:  "pv:admin:w:del:" + chat_id + ":" + word
WORD_BUDGET = 30

CLOSE_LABEL = f"[{CLOSE} بستن پنل | Close]"
BACK_LABEL = f"[{BACK} بازگشت | Back]"

# (field, emoji, persian label) - same set as the in-group panel
LOCK_TOGGLES: tuple[tuple[str, str, str], ...] = (
    ("photo_lock", "🖼", "قفل عکس"),
    ("video_lock", "🎥", "قفل ویدیو"),
    ("link_lock", "🔗", "قفل لینک"),
    ("forward_lock", "🔄", "قفل فوروارد"),
    ("emoji_lock", "😀", "قفل ایموجی"),
    ("gif_lock", "🎞", "قفل گیف"),
    ("porn_lock", "🔞", "آنتی‌پورن هوشمند"),
)


def badge(emoji: str, fa: str, en: str | None = None) -> str:
    return f"[{emoji} {fa} | {en}]" if en else f"[{emoji} {fa}]"


def btn_on(fa: str, en: str | None = None) -> str:
    return badge(GREEN, fa, en)


def btn_off(fa: str, en: str | None = None) -> str:
    return badge(RED, fa, en)


def btn_nav(fa: str, en: str | None = None) -> str:
    return badge(BLUE, fa, en)


def btn_log(fa: str, en: str | None = None) -> str:
    return badge(YELLOW, fa, en)


def btn_ai(fa: str, en: str | None = None) -> str:
    return badge(PURPLE, fa, en)


def state_badge(on: bool) -> str:
    return f"{GREEN}" if on else f"{RED}"


def _b(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _markup(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(row) for row in rows if row])


def _tail(back: str | None = None, close: bool = True) -> list[InlineKeyboardButton]:
    row: list[InlineKeyboardButton] = []
    if back:
        row.append(_b(BACK_LABEL, back))
    if close:
        row.append(_b(CLOSE_LABEL, "pv:close"))
    return [row] if row else []


def webapp_button() -> InlineKeyboardButton | None:
    """Optional glassmorphism dashboard when DASHBOARD_WEBAPP_URL is set."""
    if not settings.webapp_url:
        return None
    return InlineKeyboardButton(
        text=badge("🎨", "داشبورد گرافیکی رنگی (Web App)"),
        web_app=WebAppInfo(url=settings.webapp_url),
    )


def _with_webapp(row: list[InlineKeyboardButton]) -> list[InlineKeyboardButton]:
    extra = webapp_button()
    if extra is not None:
        row.append(extra)
    return [row] if row else []


# ---------------------------------------------------------------------------
# root menus
# ---------------------------------------------------------------------------
def god_root_kb(has_groups: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [_b(badge(BLUE, "وضعیت کل سیستم", "System Status"), "pv:god:sys")],
        [_b(badge(PURPLE, "وضعیت هوش مصنوعی و دیتاست", "AI & Harvester"), "pv:god:ai")],
        [_b(badge(BLUE, "مدیریت مدیران کل", "Group Owners"), "pv:god:owners")],
        [_b(badge(BLUE, "اطلاع‌رسانی همگانی", "Global Broadcast"), "pv:god:bcast")],
    ]
    if has_groups:
        rows.append([_b(badge(BLUE, "پنل گروه‌های من", "My Groups"), "pv:admin:groups")])
    rows += _with_webapp([])
    rows += _tail(back=None)
    return _markup(*rows)


def _addbot_button(bot_username: str) -> InlineKeyboardButton | None:
    if not bot_username:
        return None
    return InlineKeyboardButton(
        text=badge(BLUE, "افزودن ربات به گروه", "Add to Group"),
        url=f"https://t.me/{bot_username}?startgroup=true",
    )


def _support_button() -> InlineKeyboardButton | None:
    if not settings.support_url:
        return None
    return InlineKeyboardButton(
        text=badge(BLUE, "کانال پشتیبانی", "Support Channel"),
        url=settings.support_url,
    )


def admin_root_kb(bot_username: str, has_groups: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if has_groups:
        rows.append([_b(badge(BLUE, "انتخاب گروه", "Select Group"), "pv:admin:groups")])
    if (btn := _addbot_button(bot_username)) is not None:
        rows.append([btn])
    rows.append([_b(badge(BLUE, "راهنمای امکانات ربات", "Bot Help"), "pv:help")])
    if (sup := _support_button()) is not None:
        rows.append([sup])
    rows += _with_webapp([])
    rows += _tail(back=None)
    return _markup(*rows)


def user_root_kb(bot_username: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if (btn := _addbot_button(bot_username)) is not None:
        rows.append([btn])
    rows.append([_b(badge(BLUE, "راهنمای امکانات ربات", "Bot Help"), "pv:help")])
    if (sup := _support_button()) is not None:
        rows.append([sup])
    rows += _with_webapp([])
    rows += _tail(back=None)
    return _markup(*rows)


# ---------------------------------------------------------------------------
# God Admin (Level 3)
# ---------------------------------------------------------------------------
def god_status_kb() -> InlineKeyboardMarkup:
    return _markup(
        [_b(badge(BLUE, "بروزرسانی", "Refresh"), "pv:god:sys")],
        *_tail(back="pv:root"),
    )


def god_ai_kb() -> InlineKeyboardMarkup:
    return _markup(
        [_b(badge(PURPLE, "تخلیه دستی و فشرده‌سازی", "Force Flush to ZIP"), "pv:god:flush")],
        [_b(badge(BLUE, "بروزرسانی", "Refresh"), "pv:god:ai")],
        *_tail(back="pv:root"),
    )


def god_owners_kb(owners: list[tuple[int, int, str]], back: str = "pv:root") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for chat_id, user_id, title in owners[:20]:
        label = f"{NEUTRAL} {title[:30]} | {user_id}"
        rows.append([_b(label[:64], f"pv:god:ow:{user_id}:{chat_id}")])
    rows.append([_b(badge(BLUE, "افزودن مدیر گروه", "Add Group Owner"), "pv:god:ow:add")])
    rows += _tail(back=back)
    return _markup(*rows)


def owner_detail_kb(user_id: int, chat_id: int) -> InlineKeyboardMarkup:
    return _markup(
        [_b(badge(RED, "ابطال دسترسی مدیر گروه", "Revoke Owner"),
            f"pv:god:ow:del:{user_id}:{chat_id}")],
        *_tail(back="pv:god:owners"),
    )


def god_owner_step_kb() -> InlineKeyboardMarkup:
    """Cancel/back while the God Admin walks the staged assignment flow."""
    return _markup(*_tail(back="pv:god:owners"))


def god_owner_level_kb(user_id: int, chat_id: int) -> InlineKeyboardMarkup:
    """Step 3/3: pick the RBAC level for this exact (user_id, chat_id) pair."""
    return _markup(
        [_b(badge(BLUE, "ادمین گروه (Level 2)", "Group Owner"),
            f"pv:god:ow:lvl:2:{user_id}:{chat_id}")],
        [_b(badge(BLUE, "ساب‌ادمین (Level 1)", "Sub-Admin"),
            f"pv:god:ow:lvl:1:{user_id}:{chat_id}")],
        *_tail(back="pv:god:owners"),
    )


def god_broadcast_kb() -> InlineKeyboardMarkup:
    return _markup(*_tail(back="pv:root"))


# ---------------------------------------------------------------------------
# Group Owner / Sub-Admin (Level 2 / Level 1)
# ---------------------------------------------------------------------------
def group_list_kb(chats: list[tuple[int, int, str]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for chat_id, _role, title in chats[:30]:
        rows.append([_b(f"{NEUTRAL} {title[:40]}"[:64], f"pv:admin:g:{chat_id}")])
    rows.append([_b(badge(BLUE, "بروزرسانی", "Refresh"), "pv:admin:groups")])
    rows += _tail(back="pv:root")
    return _markup(*rows)


def group_panel_kb(
    chat_id: int, flags: GroupFlags, role: Role, trigger: str = DEFAULT_TRIGGER_NAME
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for field, emoji, label in LOCK_TOGGLES:
        on = flags.is_on(field)
        text = f"{emoji} {badge(GREEN if on else RED, label)}"
        rows.append([_b(text, f"pv:admin:lock:{chat_id}:{RKEY[field]}")])
    rows.append([
        _b(badge(BLUE, "کلمات ممنوعه", "Blacklist"), f"pv:admin:w:{chat_id}"),
        _b(badge(YELLOW, "لاگ‌ها و آمار", "Logs & Stats"), f"pv:admin:logs:{chat_id}"),
    ])
    second = [
        _b(badge(BLUE, "خوش‌آمد و کپچا", "Welcome & Captcha"), f"pv:admin:pol:{chat_id}"),
        _b(f"🏷 نام فراخوانی ربات: {trigger}"[:64], f"pv:admin:trg:{chat_id}"),
    ]
    if role >= Role.ADMIN:
        second.append(
            _b(badge(BLUE, "مدیریت ساب‌ادمین‌ها", "Sub-Admins"), f"pv:admin:sa:{chat_id}")
        )
    rows.append(second)
    rows += _tail(back="pv:admin:groups")
    return _markup(*rows)


def trigger_input_kb(chat_id: int) -> InlineKeyboardMarkup:
    return _markup(*_tail(back=f"pv:admin:g:{chat_id}"))


def badwords_kb(chat_id: int, words: list[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for word in words[:20]:
        rows.append([_b(f"{RED} {word}", f"pv:admin:w:del:{chat_id}:{word[:WORD_BUDGET]}")])
    rows.append([_b(badge(BLUE, "افزودن کلمات", "Add Words"), f"pv:admin:w:add:{chat_id}")])
    if words:
        rows.append([_b(badge(RED, "پاک‌کردن همه", "Clear All"), f"pv:admin:w:clr:{chat_id}")])
    rows += _tail(back=f"pv:admin:g:{chat_id}")
    return _markup(*rows)


def badwords_input_kb(chat_id: int) -> InlineKeyboardMarkup:
    return _markup(*_tail(back=f"pv:admin:w:{chat_id}"))


def subadmins_kb(chat_id: int, subs: list[int], back: str | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for uid in subs[:20]:
        rows.append([_b(f"{RED} {uid}", f"pv:admin:sa:del:{chat_id}:{uid}")])
    rows.append(
        [_b(badge(BLUE, "افزودن ساب‌ادمین", "Add Sub-Admin"), f"pv:admin:sa:add:{chat_id}")]
    )
    rows += _tail(back=back or f"pv:admin:g:{chat_id}")
    return _markup(*rows)


def policy_kb(chat_id: int, policy: GroupPolicyFlags, back: str | None = None) -> InlineKeyboardMarkup:
    captcha = (
        badge(GREEN, "کپچای عضویت فعال", "Captcha On")
        if policy.captcha_enabled
        else badge(RED, "کپچای عضویت غیرفعال", "Captcha Off")
    )
    rows: list[list[InlineKeyboardButton]] = [
        [_b(captcha, f"pv:admin:cap:{chat_id}")],
        [_b(badge(BLUE, "تنظیم پیام خوش‌آمد", "Set Welcome"), f"pv:admin:wel:{chat_id}")],
    ]
    if policy.welcome_text:
        rows.append(
            [_b(badge(RED, "حذف پیام خوش‌آمد", "Clear Welcome"), f"pv:admin:wel:x:{chat_id}")]
        )
    rows += _tail(back=back or f"pv:admin:g:{chat_id}")
    return _markup(*rows)


def logs_kb(chat_id: int) -> InlineKeyboardMarkup:
    return _markup(
        [_b(badge(YELLOW, "بروزرسانی لاگ‌ها", "Refresh Logs"), f"pv:admin:logs:{chat_id}")],
        *_tail(back=f"pv:admin:g:{chat_id}"),
    )


def confirm_clear_badwords_kb(chat_id: int) -> InlineKeyboardMarkup:
    return _markup(
        [
            _b(badge(RED, "بله، پاک شود", "Yes, clear"), f"pv:admin:w:do:{chat_id}"),
            _b(badge(NEUTRAL, "انصراف", "Cancel"), f"pv:admin:w:{chat_id}"),
        ]
    )
