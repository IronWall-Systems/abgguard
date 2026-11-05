from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from abgguard.core.rbac import Role
from abgguard.core.settings_store import RKEY, GroupFlags
from abgguard.models.entities import DEFAULT_TRIGGER_NAME

# (field, emoji, label)
TOGGLES: tuple[tuple[str, str, str], ...] = (
    ("porn_lock", "🔞", "آنتی‌پورن"),
    ("photo_lock", "🖼", "قفل عکس"),
    ("video_lock", "🎥", "قفل ویدیو"),
    ("link_lock", "🔗", "قفل لینک"),
    ("forward_lock", "🔄", "قفل فوروارد"),
    ("emoji_lock", "😀", "قفل ایموجی"),
    ("gif_lock", "🎞", "قفل گیف"),
)

# Rule 6: the closer is part of the panel itself, always visible in the group
# chat - an admin must be able to dismiss the bot's message from any screen.
CLOSE_LABEL = "[❌ بستن پنل | Close]"
CLOSE_DATA = "abg:close"


def close_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text=CLOSE_LABEL, callback_data=CLOSE_DATA)


def _rows(buttons: list[InlineKeyboardButton], per_row: int = 2) -> list[list[InlineKeyboardButton]]:
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def _with_close(rows: list[list[InlineKeyboardButton]]) -> list[list[InlineKeyboardButton]]:
    """Every group panel screen ends with its own close row (rule 6)."""
    return [*rows, [close_button()]]


def state_label(on: bool) -> str:
    """Human-readable lock state used by both group and PV panels."""
    return "🟢 فعال" if on else "🔴 غیرفعال"


def lock_label(emoji: str, label: str, on: bool) -> str:
    return f"[{emoji} {label}: {state_label(on)}]"


def panel_kb(
    flags: GroupFlags, role: Role, trigger: str = DEFAULT_TRIGGER_NAME
) -> InlineKeyboardMarkup:
    toggles = [
        InlineKeyboardButton(
            text=lock_label(emoji, label, flags.is_on(field)),
            callback_data=f"abg:lock:{RKEY[field]}",
        )
        for field, emoji, label in TOGGLES
    ]
    nav = [
        InlineKeyboardButton(text="🤬 مدیریت کلمات رکیک", callback_data="abg:bad"),
    ]
    if role >= Role.ADMIN:
        nav.append(
            InlineKeyboardButton(text="👥 مدیریت ساب‌ادمین‌ها", callback_data="abg:sa")
        )
    nav += [
        InlineKeyboardButton(
            text=f"🏷 نام فراخوانی ربات: {trigger}"[:64], callback_data="abg:trg"
        ),
        InlineKeyboardButton(text="💳 وضعیت اشتراک و تمدید", callback_data="abg:sub"),
        InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="abg:refresh"),
        InlineKeyboardButton(text="❓ راهنما", callback_data="abg:help"),
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=_with_close(_rows(toggles) + _rows(nav))
    )


def panel_help_kb(
    flags: GroupFlags, role: Role, trigger: str = DEFAULT_TRIGGER_NAME
) -> InlineKeyboardMarkup:
    """The help screen keeps the panel controls, adds Back, and keeps Close last."""
    rows = list(panel_kb(flags, role, trigger).inline_keyboard)
    rows.insert(
        -1,
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="abg:back")],
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def trigger_menu_kb() -> InlineKeyboardMarkup:
    """Back + close while typing a new panel trigger name."""
    return InlineKeyboardMarkup(
        inline_keyboard=_with_close(
            [
                [
                    InlineKeyboardButton(text="🔙 بازگشت", callback_data="abg:back"),
                ]
            ]
        )
    )


def badwords_menu_kb(words: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for w in words[:25]:
        kb.button(text=f"🗑 {w}", callback_data=f"abg:bw:del:{w[:40]}")
    kb.button(text="🗑 پاک‌کردن همه", callback_data="abg:bw:clear")
    kb.button(text="✅ پایان", callback_data="abg:back")
    kb.adjust(*([2] * len(words[:25]) + [2, 1]))
    return InlineKeyboardMarkup(
        inline_keyboard=_with_close(kb.as_markup().inline_keyboard)
    )


def subadmin_menu_kb(subs: list[int]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for uid in subs[:25]:
        kb.button(text=f"🗑 {uid}", callback_data=f"abg:sa:del:{uid}")
    kb.button(text="➕ افزودن ساب‌ادمین", callback_data="abg:sa:add")
    kb.button(text="🔙 بازگشت", callback_data="abg:back")
    kb.adjust(*([2] * len(subs[:25]) + [1, 1]))
    return InlineKeyboardMarkup(
        inline_keyboard=_with_close(kb.as_markup().inline_keyboard)
    )


def confirm_kb(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ بله", callback_data=f"abg:yes:{action}"),
                InlineKeyboardButton(text="❌ خیر", callback_data="abg:back"),
            ]
        ]
    )


def wipe_confirm_kb() -> InlineKeyboardMarkup:
    """Destructive-action guard for the full-history wipe."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="تایید ✅", callback_data="abg:wipe:yes"),
                InlineKeyboardButton(text="انصراف ❌", callback_data="abg:wipe:no"),
            ]
        ]
    )
