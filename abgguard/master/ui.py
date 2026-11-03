"""Modern Telegram UI kit for the SaaS cards (subscription, onboarding).

* `InlineKeyboardButton(copy_text=CopyTextButton(...))` (Bot API 9.1+, aiogram
  3.15) for one-tap copying of the payment card number / wallet address /
  the numeric group id.
* Direct PV deep-links (`https://t.me/abgguardbot?start=pay_{chat_id}`).
* Premium custom-emoji support: Telegram inline keyboards have no
  `icon_custom_emoji_id` field of their own, so premium (animated) emoji are
  injected into the HTML text via the `<tg-emoji>` tag. The God Admin (who has
  Telegram Premium) can plug free custom emoji ids per icon name through env
  vars (`UI_EMOJI_WALLET=...`); names without an id fall back to the standard
  glyph defined in `ICONS`.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from aiogram.types import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from abgguard.config import settings
from abgguard.core import botpair, settings_store
from abgguard.models.entities import GroupSubscription, PaymentMethod
from abgguard.services.subscription import PRICE_TEXT, STATUS_LABEL

MASTER = settings.master_username
WORKER = settings.worker_username

# direct support (owner directive): @pyyple
SUPPORT_URL = "https://t.me/pyyple"

# step-3 exact texts
PAUSED_TEXT = (
    "⏳ مدت استفاده از ربات در این گروه به اتمام رسیده است، لطفا برای استفاده مجدد آن ، "
    "اشتراک را تمدید کنید.\n"
    "⚠️ گروه تحت حفاظت گارد نمی‌باشد."
)
EXPIRED_FINAL_TEXT = PAUSED_TEXT

# standard (fallback) glyphs, overridable with custom premium emoji ids
ICONS: dict[str, str] = {
    "shield": "🛡",
    "wallet": "💳",
    "coin": "🪙",
    "gift": "🎁",
    "star": "⭐",
    "bot": "🤖",
    "group": "🏘",
    "clock": "⏳",
    "warn": "⚠️",
    "link": "🔗",
    "crown": "👑",
    "delete": "🗑",
    "mute": "🔇",
    "unmute": "🔊",
    "ban": "🚫",
    "vip": "🌟",
    "clean": "🧹",
}


def icon(name: str) -> str:
    """Fallback glyph wrapped as a Premium custom emoji when one is configured."""
    glyph = ICONS.get(name, "•")
    emoji_id = os.getenv(f"UI_EMOJI_{name.upper()}", "")
    if emoji_id.isdigit():
        return f'<tg-emoji emoji-id="{emoji_id}">{glyph}</tg-emoji>'
    return glyph


# --------------------------------------------------------------- buttons ----
def btn_copy(label: str, content: str) -> InlineKeyboardButton:
    # aiogram 3.15 (Bot API payload): copy_text carries the copied text itself
    # as `{"text": content}` - anything else raises ValidationError to Telegram.
    return InlineKeyboardButton(
        text=label, copy_text=CopyTextButton(text=content)
    )


def btn_url(label: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label, url=url)


def btn_cb(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label, callback_data=data)


def markup(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def deep_link(chat_id: int, action: str = "pay") -> str:
    """https://t.me/abgguardbot?start={action}_{chat_id}"""
    return f"https://t.me/{MASTER}?start={action}_{chat_id}"


def support_url() -> str:
    return settings.support_url or SUPPORT_URL


def support_btn() -> InlineKeyboardButton:
    return btn_url("💬 پشتیبانی", support_url())


# ------------------------------------------------------- public PV catalog ----
def _public_catalog_text(title: str) -> str:
    return (
        f"🛡 <b>{title}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>۱) معرفی و قابلیت‌های حفاظتی ربات</b>\n"
        "• 🛡 <b>هوش مصنوعی آنتی‌پورن و مدیا اسکنر:</b> شناسایی و حذف فوری "
        "تصاویر، ویدیوها، استیکرها و گیف‌های مستهجن + سکوت دائم متخلف.\n"
        "• 🤬 <b>فیلتر هوشمند کلمات رکیک و فحش:</b> تشخیص کلمات نامناسب "
        "و اعمال پلکانی اخطار و سکوت (بدون تکرار کلمه زشت در گروه).\n"
        "• 🔗 <b>قفل لینک، فوروارد، تگ و ضد اسپم (Anti-Flood):</b> "
        "جلوگیری از تبلیغات، لینک‌های تلگرامی/وب و رگبار پیام.\n"
        "• 🔨 <b>دستورات مدیریتی سریع با ریپلای (مخصوص ادمین‌ها):</b>\n"
        "   ◦ <code>حذف</code> یا <code>remove</code> — پاک‌سازی بی‌صدای پیام\n"
        "   ◦ <code>سکوت</code> یا <code>سکوت 30</code> — سکوت موقت یا دائم کاربر\n"
        "   ◦ <code>لغو سکوت</code> — رفع محدودیت کاربر\n"
        "   ◦ <code>بن</code> — اخراج دائمی از گروه (مخصوص مالک گروه)\n"
        "   ◦ <code>ویژه</code> — اعطای مجوز ارسال آزاد ۳ ساعته (VIP)\n"
        "   ◦ <code>پین</code> و <code>حذف پین</code> — سنجاق کردن یا "
        "برداشتن سنجاق پیام با ریپلای\n"
        "   ◦ <code>پاکسازی</code> (با ریپلای) — حذف دسته‌جمعی از پیام "
        "ریپلای‌شده تا اینجا\n\n"
        "<b>۲) نحوه راه‌اندازی و استفاده</b>\n"
        "<b>گام اول:</b> با استفاده از دکمه‌های زیر، <b>هر دو ربات</b> "
        f"(<code>@{MASTER or 'abgguardbot'}</code> و "
        f"<code>@{WORKER or 'ABGGUARD1BOT'}</code>) را به گروه خود اضافه "
        "کرده و دسترسی <b>Admin کامل</b> (حذف پیام، محدودسازی، پین، دعوت) "
        "بدهید. حضور ربات دوم برای اسکن مدیا و سیستم پشتیبان/Fallback الزامی است.\n"
        "<b>گام دوم:</b> اگر گروه شما <b>بالای ۵۰ عضو</b> داشته باشد، "
        "به محض ورود ربات <b>۲۴ ساعت تست رایگان</b> به صورت خودکار فعال می‌شود!\n"
        "<b>گام سوم:</b> در داخل گروه کلمه <b>«پنل»</b> را ارسال کنید یا از "
        "دکمه <b>«💳 خرید / شارژ اشتراک و گروه‌های من»</b> در همین پی‌وی، "
        "اشتراک ۳۰ روزه (۷۵۰,۰۰۰ تومان یا ۳ دلار) را تهیه و "
        "فیش را در ربات ارسال نمایید تا پس از تایید، گروه شما به صورت "
        "خودکار شارژ شود.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "برای شروع، هر دو ربات را با دکمه‌های زیر به گروه اضافه کنید. "
        "پس از افزودن، شما به‌صورت خودکار به‌عنوان مالک گروه ثبت می‌شوید "
        "و می‌توانید اشتراک تهیه یا شارژ کنید."
    )


def public_start_text() -> str:
    return _public_catalog_text("کارت معرفی و راهنمای جامع ABG Guard")


def public_help_text() -> str:
    return _public_catalog_text("راهنمای کامل دستورات و امکانات ABG Guard")


def public_mygroups_text(entries: list[tuple[int, str]]) -> str:
    if not entries:
        return (
            "💳 <b>خرید / تمدید اشتراک و گروه‌های من</b>\n\n"
            "هنوز گروهی به حساب شما متصل نیست.\n"
            "ابتدا <b>هر دو ربات</b> را با دکمه‌های زیر به گروه اضافه کنید و "
            "دسترسی ادمین بدهید. سپس همین صفحه را دوباره باز کنید تا گروه "
            "شما برای خرید و ارسال فیش نمایش داده شود.\n\n"
            "اگر قبلاً ربات را اضافه کرده‌اید، چند لحظه صبر کنید و دوباره تلاش کنید."
        )
    lines = [
        "💳 <b>خرید / تمدید اشتراک و گروه‌های من</b>",
        "",
        "گروهی را انتخاب کنید تا وضعیت اشتراک و صفحه پرداخت/ارسال فیش باز شود:",
    ]
    for chat_id, title in entries:
        lines.append(f"• <b>{title}</b> — <code>{chat_id}</code>")
    return "\n".join(lines)


# ------------------------------------------------------------------ cards ----
def status_line(status: str, expires_at) -> str:
    label = STATUS_LABEL.get(status, status)
    if status == GroupSubscription.MANUAL_WHITELIST or expires_at is None:
        return label
    remaining = (expires_at - datetime.now(UTC)).total_seconds()
    if remaining < 0:
        return f"{label} (منقضی)"
    days = remaining / 86_400
    wording = f"{days:.1f} روز" if days >= 1 else f"{max(int(remaining / 3600), 0)} ساعت"
    return f"{label} (باقی‌مانده: {wording})"


def dual_bot_warning(present: bool, is_admin: bool) -> str:
    if present and is_admin:
        return (
            f"✅ {icon('shield')} هر دو ربات با دسترسی ادمین در گروه حاضرند."
        )
    add = botpair.add_worker_url()
    if not present:
        return (
            f"⚠️ {icon('warn')} ربات دوم ({botpair.worker_label()}) در گروه نیست! "
            "برای تقسیم بار و حالت پشتیبان (Fallback) با دسترسی <b>ادمین کامل</b> "
            f"او را اضافه کنید: <a href=\"{add}\">➕ افزودن ربات دوم</a>"
        )
    return (
        f"⚠️ {icon('warn')} ربات دوم ({botpair.worker_label()}) ادمین نیست؛ "
        "ارتقای دسترسی اتنان (حذف پیام، بن، محدودسازی) الزامی است."
    )


def is_premium(status: str, expires_at) -> bool:
    """Fully-served groups: whitelisted or ACTIVE with more than 3 days left."""
    if status == GroupSubscription.MANUAL_WHITELIST:
        return True
    if status != GroupSubscription.ACTIVE:
        return False
    if expires_at is None:
        return True
    return (expires_at - datetime.now(UTC)).total_seconds() > 3 * 86_400


def tutorial_text(chat_id: int, paid: bool = False) -> str:
    """The full step-by-step Persian tutorial (spec §5).

    `paid` premium groups skip the marketing/pricing section entirely - a
    paying group must never see "750,000 Toman" or trial offers again.
    """
    add = botpair.add_worker_url()
    body = (
        f"{icon('shield')} <b>راهنمای کامل نصب و مدیریت ABG Guard</b>\n"
        f"{icon('group')} شناسه گروه: <code>{chat_id}</code>\n\n"
        "<b>۱) حضور هر دو ربات (اجباری)</b>\n"
        f"• <b>@{MASTER}</b> — ربات اصلی: پنل، دستورات و محافظت متنی.\n"
        f"• <b>@{WORKER}</b> — ربات نگهبان: آنتی‌پورن هوشمند و حالت پشتیبان.\n"
        "• هر <b>دو ربات</b> باید <b>ادمین کامل</b> باشند (حذف پیام، بن، "
        "محدودسازی، دعوت، پین) تا روی وظایف تقسیم شود و اگر سرور اصلی "
        "قطع شد، ربات دوم هم از کار افتاده‌ها مراقبت کند. "
        f"<a href=\"{add}\">➕ افزودن ربات دوم</a>\n\n"
        "<b>۲) مدیریت گروه با ریپلای (فقط ادمین‌ها)</b>\n"
        f"• {icon('delete')} <code>حذف</code> یا <code>remove</code> — پاک‌سازی "
        "بی‌صدا کردنِ پیام ریپلای‌شده.\n"
        f"• {icon('mute')} <code>سکوت</code> یا <code>/سکوت 30</code> — سکوت موقت.\n"
        f"• {icon('unmute')} <code>لغو سکوت</code> — رفع محدودیت.\n"
        f"• {icon('ban')} <code>بن</code> — حذف اعضای کاربر (نیاز به سطح ۲).\n"
        f"• {icon('vip')} <code>ویژه</code> — سه ساعت مجوز ارسال آزاد.\n"
        f"• {icon('clean')} <code>پاکسازی</code> (با ریپلای) — حذف پیام‌ها "
        "از پیام ریپلای‌شده تا اینجا.\n"
    )
    if not paid:
        body += (
            "\n<b>۳) اشتراک و بودجه‌گذاری</b>\n"
            f"• 💳 اشتراک ماهانه: <b>{PRICE_TEXT}</b> یا <b>${settings.price_usd}</b>.\n"
            f"• ⏱ دوره اشتراک: <b>{settings.cycle_days}</b> روز؛ تمدید با پیوند در "
            f"پایین همین کارت.\n"
            f"• 🎁 <b>تست رایگان ۲۴ ساعته</b> — فقط و فقط برای گروه‌های "
            f"<b>بالای {settings.trial_min_members} عضو</b> و یک‌بار.\n"
        )
    body += (
        "\n📁 پنل مدیریت: در چت گروه «پنل» را بنویسید؛ در پیوی هم "
        "<code>/start</code> بزنید."
    )
    return body


def subscribe_text(chat_id: int, title: str, status: str, expires_at, count: int = 0) -> str:
    member = f"\n👥 اعضا: <code>{count}</code>" if count else ""
    pricing = ""
    if not is_premium(status, expires_at):
        pricing = (
            f"💳 تعرفه اشتراک {settings.cycle_days} روزه: <b>{PRICE_TEXT}</b> | "
            f"<b>${settings.price_usd}</b>\n"
            f"🎁 تست رایگان ۲۴ ساعته: فقط گروه‌های بالای "
            f"<b>{settings.trial_min_members} عضو</b> (یک‌بار)\n"
            f"⏳ پس از پرداخت، رسید را برای مدیریت بفرستید تا اشتراک فعال شود."
        )
    return (
        f"{icon('wallet')} <b>کارت اشتراک ABG Guard</b>\n"
        f"{icon('group')} گروه: <b>{title}</b>\n"
        f"🆔 شناسه گروه: <code>{chat_id}</code>{member}\n"
        f"📅 وضعیت: {status_line(status, expires_at)}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        + pricing
    ).rstrip()


def _payment_rows(chat_id: int, paid: bool = False) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    if settings.payment_card_number:
        holder = settings.payment_card_holder or "نام‌نگار اعتباری"
        rows.append([
            btn_copy(f"💳 کپی شماره کارت ({holder[:20]})", settings.payment_card_number)
        ])
    if settings.payment_crypto_address:
        rows.append([
            btn_copy("🪙 کپی کیف پول USDT (TRC20)", settings.payment_crypto_address)
        ])
    if not paid:
        rows.append([
            btn_cb("⭐️ پرداخت و تمدید", f"sub:pay:{chat_id}")
        ])
    rows.append([support_btn()])
    return rows


def renew_kb(chat_id: int) -> InlineKeyboardMarkup:
    """Payment details + Support, right where the admin is standing."""
    return subscribe_kb(chat_id)


def subscribe_kb(chat_id: int, paid: bool = False) -> InlineKeyboardMarkup:
    rows = _payment_rows(chat_id, paid=paid)
    rows.append([btn_cb("🔄 بارگذاری دوباره وضعیت", f"sub:menu:{chat_id}")])
    return markup(*rows)


def onboarding_kb(chat_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [btn_url(f"➕ افزودن @{WORKER} با دسترسی ادمین", botpair.add_worker_url())],
        [btn_copy(f"🆔 کپی شناسه گروه ({chat_id})", str(chat_id))],
    ]
    rows += _payment_rows(chat_id)
    rows.append([btn_cb("🔄 بارگذاری دوباره وضعیت", f"sub:menu:{chat_id}")])
    return markup(*rows)


def trial_activated(chat_id: int, title: str, hours: int, present: bool, is_admin: bool) -> str:
    return (
        f"{icon('gift')} <b>تست رایگان ۲۴ ساعته فعال شد!</b>\n"
        f"{icon('group')} گروه: <b>{title}</b>\n"
        f"⏳ مدت: <b>{hours} ساعت</b>\n\n"
        + dual_bot_warning(present, is_admin)
        + "\n\n"
        + tutorial_text(chat_id)
    )


def subscription_required(
    chat_id: int, title: str, count: int, present: bool, is_admin: bool
) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"{icon('shield')} <b>ABG Guard فعال شد!</b>\n"
        f"{icon('group')} گروه: <b>{title}</b>\n\n"
        + dual_bot_warning(present, is_admin)
        + "\n\n"
        + f"{icon('warn')} اشتراک این گروه فعال نیست و محافظت موقتاً متوقف است.\n"
        f"💳 اشتراک ۳۰ روزه: <b>{PRICE_TEXT}</b> یا <b>${settings.price_usd}</b>\n"
        f"🎁 تست رایگان ۲۴ ساعته: فقط گروه‌های بالای "
        f"<b>{settings.trial_min_members} عضو</b> (یک‌بار)\n\n"
        + tutorial_text(chat_id)
    )
    return text, onboarding_kb(chat_id)


def trial_expired(chat_id: int, title: str, present: bool, is_admin: bool) -> str:
    return (
        f"{icon('clock')} <b>تست رایگان تمام شد.</b>\n"
        f"{icon('group')} گروه: <b>{title}</b>\n\n"
        + dual_bot_warning(present, is_admin)
        + "\n\n"
        + f"💳 برای ادامه محافظت اشتراک ۳۰ روزه فعال کنید "
        f"(<b>{PRICE_TEXT}</b> یا <b>${settings.price_usd}</b>).\n\n"
        + tutorial_text(chat_id)
    )


def active_state_text(
    chat_id: int, title: str, status: str, expires_at, present: bool, is_admin: bool
) -> str:
    return (
        f"{icon('shield')} <b>ABG Guard در خدمت شماییم.</b>\n"
        f"{icon('group')} گروه: <b>{title}</b>\n"
        f"📅 وضعیت اشتراک: {status_line(status, expires_at)}\n\n"
        + dual_bot_warning(present, is_admin)
        + "\n\n"
        + tutorial_text(chat_id, paid=is_premium(status, expires_at))
    )


def pay_view_text(chat_id: int, title: str, status: str, expires_at) -> str:
    """The private-chat view behind `?start=pay_{chat_id}` deep-links."""
    text = subscribe_text(chat_id, title, status, expires_at)
    return text.strip() + "\n\n" + tutorial_text(
        chat_id, paid=is_premium(status, expires_at)
    )


def pay_screen_text(chat_id: int, title: str, status: str, expires_at) -> str:
    """Dedicated payment screen - NO install tutorial, NO membership sweep."""
    return (
        f"💳 <b>صفحه پرداخت و تمدید اشتراک</b>\n\n"
        f"🏘 گروه: <b>{title}</b>\n"
        f"🆔 شناسه گروه: <code>{chat_id}</code>\n"
        f"📅 وضعیت فعلی اشتراک: {status_line(status, expires_at)}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 مبلغ قابل پرداخت (ماهیانه {settings.cycle_days} روز):\n"
        f"• <b>{PRICE_TEXT}</b> (ریال/تومان)\n"
        f"• <b>۳ دلار (USDT - ترانک ۲۰)</b>\n\n"
        f"🧾 پس از پرداخت، فیش را با دکمه «ارسال فیش پرداخت» بفرستید "
        f"تا اشتراک ۳۰ روزه گروه با تایید مدیریت فعال شود."
    )


async def _gateway_rows() -> list[list[InlineKeyboardButton]]:
    """One 1-tap CopyTextButton per active God-managed gateway (DB first)."""
    rows: list[list[InlineKeyboardButton]] = []
    try:
        methods = await settings_store.payment_list(active_only=True)
    except Exception:  # billing data unreadable - env fallback instead
        methods = []
    for method in methods:
        if method.type == PaymentMethod.CRYPTO:
            label = f"🪙 کپی آدرس ولت ({method.title})"[:60]
        else:
            label = f"💳 کپی شماره کارت ({method.title})"[:60]
        rows.append([btn_copy(label, method.value)])
    return rows


async def pay_screen_kb(
    chat_id: int, *, back: str | None = None
) -> InlineKeyboardMarkup:
    """CopyText card/wallet rows + in-bot receipt + back-to-panel. NO urls."""
    rows = await _gateway_rows()
    rows.append([btn_cb("📤 ارسال فیش پرداخت (در همین ربات)", f"sub:receipt:{chat_id}")])
    rows.append([btn_url("💬 پشتیبانی", support_url())])
    rows.append(
        [btn_cb("🔙 بازگشت", back or f"sub:menu:{chat_id}")]
    )
    return markup(*rows)
