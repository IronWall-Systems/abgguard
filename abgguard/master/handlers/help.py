from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from abgguard.config import settings
from abgguard.core import rbac
from abgguard.core.rbac import Role
from abgguard.master import pv_keyboards as pv_kb
from abgguard.master import ui
from abgguard.master.ephemeral import delete_later, fa_num, spawn
from abgguard.services.textguards import is_bot_message

log = logging.getLogger("abgguard.master.help")

router = Router(name="master.help")

NS = "help"  # callback namespace: help:home | help:idx:<n> | help:pg:<key> | help:close
PAGE_SIZE = 5  # sections per menu page
DELETE_AFTER = 10.0  # lifetime of the /help trigger inside a group
GUIDE_AFTER = 60.0  # lifetime of the flat guide inside a group (owner spec)

BACK_LABEL = "[🔙 بازگشت | Back]"
CLOSE_LABEL = "[❌ بستن | Close]"
PV_LABEL = "[💬 راهنما در پی‌وی | Help in PV]"
ADDBOT_LABEL = "[➕ افزودن ربات به گروه | Add to Group]"
SUPPORT_LABEL = "[📣 کانال پشتیبانی | Support]"

GROUP_TRIGGER = r"^/start(?:@\w+)?[ \t]+help[ \t]*$"


# ---------------------------------------------------------------------------
# content model - every page is a plain, side-effect free value so the whole
# menu can be built (and unit tested) without Telegram, Redis or PostgreSQL.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Section:
    key: str
    icon: str
    title: str
    title_en: str
    body: str
    min_role: Role = Role.USER

    @property
    def button(self) -> str:
        return f"[{self.icon} {self.title} | {self.title_en}]"


SECTIONS: dict[str, Section] = {
    s.key: s
    for s in (
        Section(
            key="about",
            icon="ℹ️",
            title="درباره ABG Guard",
            title_en="About",
            min_role=Role.USER,
            body=(
                "🛡 ABG Guard چیست؟\n\n"
                "ربات محافظ گروه شماست؛ کارها را بی‌صدا انجام می‌دهد و چت را شلوغ نمی‌کند.\n\n"
                "✨ امکانات:\n"
                "• قفل‌های هوشمند: عکس 🖼، ویدیو 🎥، گیف 🎞، لینک 🔗، فوروارد 🔄، ایموجی 😀\n"
                "• آنتی‌پورن هوشمند 🔞 با هوش مصنوعی محلی، بدون هیچ اتصال به اینترنت\n"
                "• جلوگیری از اسپم و فلود، با اخطار و سکوت تدریجی\n"
                "• کپچای عضویت و پیام خوش‌آمد برای اعضای جدید\n"
                "• فهرست کلمات ممنوعه، جدا برای هر گروه\n\n"
                "🚀 شروع به‌کار:\n"
                "۱. ربات را به گروه خود اضافه کنید.\n"
                "۲. مدیر گروه سطح خود را با <code>/whoami</code> می‌بیند و با نوشتن «پنل» "
                "یا دستور <code>/panel</code> پنل مدیریت را باز می‌کند.\n"
                "۳. نام فراخوانی سفارشی: با دکمهٔ «🏷 نام فراخوانی ربات» در پنل، "
                "می‌توان «پنل» را به نام دلخواه ۲ تا ۲۰ کاراکتری تغییر داد.\n\n"
                "🌟 کاربر ویژه (۳ ساعت):\n"
                "ریپلای روی پیام عضو + نوشتن «ویژه» یا «وی‌آی‌پی» ← او ۳ ساعت از "
                "قفل‌های رسانه مستثنا می‌شود؛ «لغو ویژه» برای لغو.\n\n"
                "🔎 دستورات عمومی:\n"
                "<code>/whoami</code> — نمایش سطح دسترسی شما\n"
                "<code>/help</code> — همین راهنما\n\n"
                "برای عضویت در گروه یا پشتیبانی، از دکمه‌های پایین استفاده کنید."
            ),
        ),
        Section(
            key="law",
            icon="⚖️",
            title="اعمال قانون",
            title_en="Moderation",
            min_role=Role.SUBADMIN,
            body=(
                "دستورات با ریپلای روی پیام متخلف اجرا می‌شود "
                "(یا با نوشتن شناسهٔ عددی کاربر بعد از دستور).\n\n"
                "🔇 سکوت (میوت)\n"
                "<code>/mute</code> یا <code>/سکوت</code>\n"
                "مدت دلخواه: <code>/mute 30</code> یعنی ۳۰ دقیقه، "
                "<code>/mute 2h</code> یعنی ۲ ساعت، <code>/mute 45s</code> یعنی ۴۵ ثانیه.\n"
                "پیش‌فرض: ۱۰ دقیقه.\n\n"
                "🔇 سکوت دائم (بدون تاریخ پایان)\n"
                "<code>/full_mute</code>\n"
                "کاربر در گروه می‌ماند و فقط حق ارسال ندارد؛ بنابراین برخلاف بن، "
                "با عضویت دوباره هم برنمی‌گردد.\n\n"
                "🔊 لغو سکوت\n"
                "<code>/unmute</code>، «لغو سکوت» یا «حذف سکوت»\n"
                "حق ارسال پیش‌فرض کاربر را برمی‌گرداند.\n\n"
                "🚫 بن\n"
                "<code>/ban</code> یا <code>/بن</code>\n"
                "اخراج دائمی کاربر از گروه.\n"
                "⚠️ فقط مالک گروه (سطح ۲) و گاد ادمین (سطح ۳) می‌توانند بن بزنند؛ "
                "ساب‌ادمین (سطح ۱) پیام «شما دسترسی بن کردن ندارید» می‌گیرد.\n"
                "⚠️ ربات هرگز خودش بن نمی‌زند؛ در سقف اخطار یا تخلف بحرانی، "
                "کاربر برای همیشه سکوت می‌شود و تصمیم نهایی به مالک گروه "
                "در پیوی اطلاع داده می‌شود.\n\n"
                "⚠️ اخطار\n"
                "<code>/warn</code> یا <code>/اخطار</code>\n"
                "شمارشگر اخطار مشترک با سیستم خودکار است:\n"
                "• اخطار اول ← ۱۰ دقیقه سکوت\n"
                "• اخطار دوم ← سکوت دائم + ارجاع به مالک گروه\n\n"
                "🧹 پاکسازی\n"
                "<code>/purge [تعداد]</code> ← حذف پیام‌های اخیر "
                "(پیش‌فرض ۲۰، حداکثر ۱۰۰).\n"
                "<code>/wipe</code> یا «پاکسازی» با ریپلای ← حذف همهٔ پیام‌ها "
                "از پیام ریپلای‌شده تا همین دستور.\n"
                "<code>/wipeall</code> یا «پاکسازی همه» ← پاکسازی پنجرهٔ "
                "امن ۲۰۰ پیام اخیر (محدودیت ۴۸ ساعتهٔ Bot API).\n\n"
                "💡 خود دستور و پاسخ ربات خودبه‌خود بعد از ۱۰ ثانیه پاک می‌شود.\n"
                "💡 برای تیم مدیریت، نوشتن تک‌کلمهٔ «سکوت» یا «اخطار» هم اجرا می‌شود."
            ),
        ),
        Section(
            key="locks",
            icon="🛡",
            title="قفل‌ها و امنیت",
            title_en="Locks & Security",
            min_role=Role.SUBADMIN,
            body=(
                "هر قفل یک کلید روشن/خاموش است:\n"
                "✅ قفل فعال (پیام ناقض حذف می‌شود)    ❌ قفل غیرفعال\n\n"
                "📌 روش اول — پنل داخل گروه:\n"
                "۱. در گروه بنویسید: پنل\n"
                "۲. روی دکمهٔ موردنظر بزنید؛ تغییر بلافاصله ذخیره می‌شود.\n\n"
                "📌 روش دوم — از پیوی:\n"
                "پنل ← انتخاب گروه ← همان دکمه‌ها (تنظیم از راه دور)\n\n"
                "فهرست قفل‌ها:\n"
                "🔞 آنتی‌پورن هوشمند — اسکن محلی تصاویر و ویدیوها با هوش مصنوعی\n"
                "🖼 قفل عکس    🎥 قفل ویدیو    🎞 قفل گیف\n"
                "😀 قفل ایموجی — پیامی که فقط ایموجی دارد حذف می‌شود\n"
                "🔗 قفل لینک — ارسال لینک ممنوع و ۵ دقیقه سکوت\n"
                "🔄 قفل فوروارد — پیام فورواردشده حذف می‌شود\n\n"
                "💡 وضعیت هر قفل در پنل با ✅ و ❌ نشان داده می‌شود."
            ),
        ),
        Section(
            key="pvdash",
            icon="📱",
            title="داشبورد ریموت PV",
            title_en="Remote Dashboard",
            min_role=Role.SUBADMIN,
            body=(
                "همهٔ تنظیمات را می‌توانید از پیوی انجام دهید؛ گروه تمیز می‌ماند.\n\n"
                "🚀 باز کردن پنل:\n"
                "• در پیوی ربات: <code>/start</code> یا <code>/panel</code>\n"
                "• داخل گروه: دکمهٔ «راهنما در پی‌وی» یا نوشتن «پنل»\n\n"
                "🎨 رنگ دکمه‌ها:\n"
                "🟢 فعال و سالم    🔴 غیرفعال یا خطر\n"
                "🔵 ناوبری و عملیات    🟡 لاگ‌ها و هشدار\n"
                "🟣 هوش مصنوعی و دیتاست    ❌ بستن پنل\n\n"
                "🗂 مسیرها:\n"
                "«انتخاب گروه» ← گروه موردنظر ← قفل‌ها، کلمات ممنوعه، "
                "خوش‌آمد و کپچا، لاگ‌ها و آمار\n\n"
                "💡 همهٔ صفحه‌ها در همان پیام باز می‌شوند؛ با "
                "<code>[🔙 بازگشت | Back]</code> به عقب و با "
                "<code>[❌ بستن | Close]</code> ببندید."
            ),
        ),
        Section(
            key="blacklist",
            icon="🤬",
            title="بلک‌لیست کلمات",
            title_en="Blacklist",
            min_role=Role.SUBADMIN,
            body=(
                "فهرست کلمات ممنوعه، جدا برای هر گروه است.\n\n"
                "➕ افزودن:\n"
                "۱. در پنل گروه روی «کلمات رکیک» بزنید.\n"
                "۲. کلمات را با کاما بفرستید: بد, فحش, توهین\n"
                "۳. یا از پیوی: انتخاب گروه ← «کلمات ممنوعه»\n\n"
                "🗑 حذف:\n"
                "روی هر کلمه در فهرست بزنید تا حذف شود، "
                "یا «پاک‌کردن همه» را بزنید.\n\n"
                "⚖️ برخورد خودکار با متخلف:\n"
                "• اخطار اول ← ۱۰ دقیقه سکوت\n"
                "• اخطار دوم ← سکوت دائم + ارجاع به مالک گروه\n"
                "(پیام حذف و هرگز وارد دیتاست نمی‌شود)"
            ),
        ),
        Section(
            key="subadmins",
            icon="👥",
            title="مدیریت ساب‌ادمین‌ها",
            title_en="Sub-Admins",
            min_role=Role.ADMIN,
            body=(
                "ساب‌ادمین (Level 1) می‌تواند قفل‌ها را عوض کند، کلمات ممنوعه را "
                "مدیریت کند و از دستورات اعمال قانون استفاده کند؛ اما نمی‌تواند "
                "ساب‌ادمین دیگری اضافه یا حذف کند.\n\n"
                "⚖️ مقایسهٔ ساب‌ادمین و مالک گروه:\n"
                "• ساب‌ادمین (سطح ۱): اخطار، سکوت، سکوت دائم، لغو سکوت، پاکسازی، تغییر قفل‌ها "
                "و کلمات ممنوعه ← <b>بن نمی‌زند</b>.\n"
                "• مالک گروه (سطح ۲): همهٔ موارد بالا + بن + افزودن و حذف ساب‌ادمین.\n"
                "• گاد ادمین (سطح ۳): مدیریت مالکان گروه‌ها و کل سیستم.\n"
                "تلاش ساب‌ادمین برای بن با پیام «⚠️ شما دسترسی بن کردن ندارید.» رد می‌شود.\n\n"
                "➕ افزودن (فقط Level 2 و بالاتر):\n"
                "• ریپلای روی کاربر + <code>/addsub</code>\n"
                "• یا <code>/addsub 123456789</code>\n"
                "• یا از پیوی ← «مدیریت ساب‌ادمین‌ها»\n\n"
                "🗑 حذف:\n"
                "ریپلای + <code>/rmsub</code> یا <code>/rmsub 123456789</code>\n\n"
                "💡 سطح دسترسی خود و دیگران را با <code>/whoami</code> ببینید."
            ),
        ),
        Section(
            key="settings",
            icon="⚙️",
            title="تنظیمات گروه",
            title_en="Group Settings",
            min_role=Role.SUBADMIN,
            body=(
                "🔎 سطح دسترسی خود را ببینید:\n"
                "<code>/whoami</code>\n\n"
                "📋 پنل گروه:\n"
                "کلمهٔ «پنل» یا <code>/panel</code> ← قفل‌ها، کلمات رکیک و ساب‌ادمین‌ها\n\n"
                "🏷 نام فراخوانی سفارشی:\n"
                "دکمهٔ «نام فراخوانی ربات» در پنل ← نام دلخواه ۲ تا ۲۰ کاراکتری "
                "(پیش‌فرض «پنل») که همان کار پنل را انجام می‌دهد.\n\n"
                "👋 خوش‌آمد و کپچا (از پیوی):\n"
                "پنل ← انتخاب گروه ← «خوش‌آمد و کپچا»\n"
                "متغیرهای مجاز در پیام خوش‌آمد: <code>{mention}</code> ، "
                "<code>{name}</code> ، <code>{id}</code>\n\n"
                "📊 لاگ اقدامات:\n"
                "پنل ← انتخاب گروه ← «لاگ‌ها و آمار»\n"
                "فهرست بن‌ها و میوت‌های اخیر همراه با دلیل و زمان.\n\n"
                "🗑 عضویت:\n"
                "سیل عضویت فقط گروه را موقتاً قفل می‌کند؛ هیچ‌کس به‌خاطر عضویت بن نمی‌شود."
            ),
        ),
        Section(
            key="sys",
            icon="📊",
            title="مدیریت سیستم و آمار",
            title_en="System & Stats",
            min_role=Role.GOD,
            body=(
                "از پیوی ← «وضعیت کل سیستم»:\n"
                "🖥 بار پردازنده، حافظهٔ میزبان و کانتینر\n"
                "🐳 وضعیت کانتینرها و مصرف منابع\n"
                "🛢 اتصال PostgreSQL و Redis\n"
                "⚖️ آمار اقدامات (بن و میوت)، اعضا و گروه‌های محافظت‌شده\n\n"
                "👑 مدیریت سطح گاد ادمین:\n"
                "<code>/god &lt;user_id&gt;</code> — افزودن گاد ادمین\n"
                "<code>/ungod &lt;user_id&gt;</code> — حذف گاد ادمین\n"
                "(حذف خودتان ممکن نیست)\n\n"
                "📣 اطلاع‌رسانی همگانی:\n"
                "پیوی ← «اطلاع‌رسانی همگانی» ← متن پیام را بفرستید؛ "
                "برای همهٔ گروه‌های ثبت‌شده ارسال می‌شود.\n\n"
                "💡 برای مدیریت گروه‌ها از «پنل گروه‌های من» یا نوشتن «پنل» داخل گروه استفاده کنید."
            ),
        ),
        Section(
            key="owners",
            icon="👑",
            title="مدیران کل گروه‌ها",
            title_en="Group Owners",
            min_role=Role.GOD,
            body=(
                "مالک گروه (Level 2) می‌تواند همهٔ تنظیمات گروه را عوض کند و "
                "ساب‌ادمین اضافه کند. نیازی نیست عضو گروه باشد؛ فقط «شناسهٔ عددی» "
                "کاربر و «شناسهٔ گروه» لازم است.\n\n"
                "➕ افزودن مالک گروه:\n"
                "<code>/addadmin &lt;user_id&gt; &lt;chat_id&gt;</code>\n"
                "• یا داخل گروه، ریپلای روی کاربر + <code>/addadmin</code>\n"
                "• یا از پیوی ← «مدیریت مدیران کل» ← «افزودن مدیر گروه»\n\n"
                "🗑 حذف مالک گروه:\n"
                "<code>/deladmin &lt;user_id&gt; &lt;chat_id&gt;</code>\n"
                "یا از همان صفحه، روی مورد مربوط بزنید.\n\n"
                "🔎 دیدن فهرست و شناسهٔ گروه‌ها:\n"
                "پیوی ← «مدیریت مدیران کل»؛ شناسهٔ گروه کنار هر نوشته آمده است."
            ),
        ),
        Section(
            key="harvester",
            icon="🟣",
            title="هاروستر دادهٔ ML",
            title_en="ML Harvester",
            min_role=Role.GOD,
            body=(
                "ABG Guard پیام‌های تمیز گروه‌ها را به‌صورت جفت "
                "«متن اصلی + پاسخ» جمع می‌کند تا مدل زبانی آموزش ببیند. "
                "پیام‌هایی که توسط قفل‌ها حذف شده‌اند هرگز ثبت نمی‌شوند.\n\n"
                "از پیوی ← «وضعیت هوش مصنوعی و دیتاست»:\n"
                "🧠 سلامت سرویس اسکن NudeNet\n"
                "📚 بافر فعال، تعداد رکوردها و رکوردهای از‌دست‌رفته\n"
                "🗜 آرشیو‌های ZIP و تعداد چرخش‌ها\n\n"
                "♻️ چرخش خودکار:\n"
                "به محض رسیدن بافر به ۵۰ مگابایت، فایل فشرده و در پوشهٔ "
                "<code>dataset</code> ذخیره می‌شود و فایل تازه‌ای آغاز می‌شود؛ "
                "هیچ رکوردی در این فاصله گم نمی‌شود.\n\n"
                "🟣 دکمهٔ «تخلیه دستی و فشرده‌سازی» همین کار را زودتر انجام می‌دهد.\n\n"
                "🧠 سرویس اسکن تصویر فقط روی همین سرور اجرا می‌شود "
                "و تصاویر خلاف قاعده را پیش از دیده‌شدن حذف می‌کند."
            ),
        ),
    )
}

# ordered sections per role - most relevant first (spec: role-aware help)
_MENU_KEYS: dict[Role, tuple[str, ...]] = {
    Role.GOD: (
        "sys", "owners", "harvester",
        "pvdash", "blacklist", "subadmins",
        "law", "locks", "settings", "about",
    ),
    Role.ADMIN: (
        "pvdash", "blacklist", "subadmins",
        "law", "locks", "settings", "about",
    ),
    Role.SUBADMIN: ("law", "locks", "pvdash", "settings", "about"),
    Role.USER: ("about",),
}


# ------------------------------------------------------------------- pure ----
def menu_keys(role: Role) -> tuple[str, ...]:
    for candidate in (Role.GOD, Role.ADMIN, Role.SUBADMIN):
        if role >= candidate:
            return _MENU_KEYS[candidate]
    return _MENU_KEYS[Role.USER]


def menu_for(role: Role) -> tuple[Section, ...]:
    return tuple(SECTIONS[key] for key in menu_keys(role))


def page_count(total: int) -> int:
    return max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)


def page_slice(role: Role, page: int) -> tuple[Section, ...]:
    items = menu_for(role)
    pages = page_count(len(items))
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    return items[start : start + PAGE_SIZE]


def resolve(data: str | None) -> tuple[str, str]:
    """``("pg", "law")`` for ``help:pg:law`` - ``("", "")`` when unknown."""
    parts = (data or "").split(":")
    if len(parts) < 2 or parts[0] != NS:
        return "", ""
    action = parts[1]
    if action in {"home", "close"}:
        return action, ""
    if action == "idx" and len(parts) > 2 and parts[2].isdigit():
        return action, parts[2]
    if action == "pg" and len(parts) > 2 and ":".join(parts[2:]) in SECTIONS:
        return action, ":".join(parts[2:])
    return "", ""


def menu_text(role: Role, page: int = 0) -> str:
    items = menu_for(role)
    pages = page_count(len(items))
    page = max(0, min(page, pages - 1))
    lines = [
        "👋 به سیستم راهنمای ABG Guard خوش آمدید!",
        "",
        "❓ راهنمای ABG Guard",
        "",
        f"🔰 سطح دسترسی شما: {role.label_fa} (Level {int(role)})",
        "یکی از بخش‌های زیر را انتخاب کنید تا راهنمای کامل آن باز شود.",
    ]
    if pages > 1:
        lines.append(f"📄 صفحهٔ {fa_num(page + 1)} از {fa_num(pages)}")
    if role is Role.USER:
        lines += ["", "برای عضویت در گروه یا پشتیبانی، از دکمه‌های پایین کمک بگیرید."]
    return "\n".join(lines)


def section_text(section: Section) -> str:
    return f"{section.icon} {section.title}\n{'─' * 18}\n{section.body}"


def guide_text() -> str:
    """The flat Persian guide `/help` posts inside a group.

    One self-contained HTML message: panel (incl. the custom trigger name),
    the moderation commands, the 3-hour VIP and the permission hierarchy.
    It never contains the interactive menu (rule 6 - the group stays clean)
    and the caller deletes it after ``GUIDE_AFTER`` seconds.
    """
    return (
        "<b>📖 راهنمای فارسی ABG Guard</b>\n"
        f"{'─' * 22}\n"
        "این پیام خودبه‌خود بعد از ۶۰ ثانیه پاک می‌شود تا گروه تمیز بماند.\n\n"
        "<b>۱) باز کردن پنل ⚙️</b>\n"
        "• داخل گروه: کلمهٔ «پنل» یا دستور <code>/panel</code> را بفرستید.\n"
        "• نام فراخوانی سفارشی: مالک گروه از دکمهٔ «🏷 نام فراخوانی ربات» در پنل، "
        "نام دلخواه ۲ تا ۲۰ کاراکتری (مثلاً «راهنما») انتخاب می‌کند و همان کلمه پنل را باز می‌کند.\n"
        "• از پیوی ربات: <code>/start</code> یا <code>/panel</code> ← انتخاب گروه (تنظیم از راه دور).\n\n"
        "<b>۲) دستورات اعمال قانون ⚖️</b>\n"
        "با ریپلای روی پیام متخلف (یا بعد از دستور، شناسهٔ عددی کاربر):\n"
        "• <code>/اخطار</code> یا <code>/warn</code> ← اخطار ۱: سکوت ۱۰ دقیقه، "
        "اخطار ۲: سکوت دائم + ارجاع به مالک گروه\n"
        "• <code>/سکوت</code> یا <code>/mute</code> ← مدت دلخواه: <code>/mute 30</code> یا <code>/mute 2h</code>\n"
        "• <code>/full_mute</code> ← سکوت دائم، بدون تاریخ پایان\n"
        "• <code>/unmute</code> یا «لغو سکوت» ← لغو سکوت کاربر\n"
        "• <code>/بن</code> یا <code>/ban</code> ← فقط مالک گروه و گاد ادمین؛ "
        "ربات هرگز خودش بن نمی‌زند\n"
        "• <code>/purge [تعداد]</code> ← پاک کردن پیام‌های اخیر؛ "
        "<code>/wipe</code> یا «پاکسازی» با ریپلای ← پاک‌سازی بازه‌ای؛ "
        "<code>/wipeall</code> یا «پاکسازی همه» ← پاک‌سازی پنجرهٔ امن اخیر\n"
        "💡 خود دستور و پاسخ ربات در همان لحظه حذف می‌شود (پروتکل چت تمیز).\n\n"
        "<b>۳) کاربر ویژه ۳ ساعته 🌟</b>\n"
        "ریپلای روی پیام عضو + نوشتن «ویژه» یا «وی‌آی‌پی» ← او ۳ ساعت از قفل‌ها مستثنا "
        "می‌شود و می‌تواند رسانه بفرستد.\n"
        "برای لغو: ریپلای + «لغو ویژه» یا «لغو وی‌آی‌پی».\n\n"
        "<b>۴) سطوح دسترسی 🔐</b>\n"
        "• سطح ۱ — ساب‌ادمین: اخطار، سکوت، سکوت دائم، لغو سکوت، پاکسازی و تغییر قفل‌ها؛ "
        "<b>دسترسی بن ندارد</b>.\n"
        "• سطح ۲ — مالک گروه: همهٔ دستورات بالا + بن + افزودن ساب‌ادمین.\n"
        "• سطح ۳ — گاد ادمین: مدیریت کل سیستم، مالکان گروه‌ها و اطلاع‌رسانی همگانی.\n\n"
        "💡 سطح خود را با <code>/whoami</code> ببینید؛ راهنمای کامل و تعاملی در پیوی ربات "
        "باقی می‌ماند."
    )


# --------------------------------------------------------------- keyboards ---
def _b(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _extra_buttons(role: Role, key: str, bot_username: str) -> list[list[InlineKeyboardButton]]:
    """Support / add-to-group links: the whole menu for Level 0, plus the page."""
    wanted = key == "about" or (not key and role is Role.USER)
    if not wanted:
        return []
    rows: list[list[InlineKeyboardButton]] = []
    if bot_username:
        rows.append(
            [
                InlineKeyboardButton(
                    text=ADDBOT_LABEL, url=f"https://t.me/{bot_username}?startgroup=true"
                )
            ]
        )
    if settings.support_url:
        rows.append(
            [InlineKeyboardButton(text=SUPPORT_LABEL, url=settings.support_url)]
        )
    return rows


def _tail(back: str) -> list[list[InlineKeyboardButton]]:
    return [[_b(BACK_LABEL, back), _b(CLOSE_LABEL, f"{NS}:close")]]


def menu_kb(
    role: Role, page: int = 0, bot_username: str = ""
) -> InlineKeyboardMarkup:
    items = menu_for(role)
    pages = page_count(len(items))
    page = max(0, min(page, pages - 1))

    rows = [
        [_b(section.button, f"{NS}:pg:{section.key}")]
        for section in page_slice(role, page)
    ]
    if pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(_b("◀️", f"{NS}:idx:{page - 1}"))
        if page < pages - 1:
            nav.append(_b("▶️", f"{NS}:idx:{page + 1}"))
        rows.append(nav)
    rows += _extra_buttons(role, "", bot_username)
    rows += _tail("pv:root")
    return InlineKeyboardMarkup(inline_keyboard=rows)


def page_kb(
    section: Section, role: Role = Role.USER, bot_username: str = ""
) -> InlineKeyboardMarkup:
    rows = _extra_buttons(role, section.key, bot_username)
    rows += _tail(f"{NS}:home")
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ------------------------------------------------------------------ utils ----
async def _ack(cq: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
    try:
        await cq.answer(text, show_alert=alert)
    except Exception:  # already answered / query too old
        pass


async def _edit(cq: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    """In-place update (project rule 6: never spam a new message per press)."""
    msg = cq.message
    if not isinstance(msg, Message) or msg.chat is None:
        return
    try:
        await msg.edit_text(text, reply_markup=markup)
    except Exception as exc:  # "message is not modified" and friends
        log.debug("help edit skipped: %s", exc)


_bot_username_cache: str | None = None


async def _bot_username(bot: Bot) -> str:
    global _bot_username_cache
    if not _bot_username_cache:
        try:
            me = await bot.me()
            _bot_username_cache = me.username or ""
        except Exception:
            log.debug("bot.me() failed", exc_info=True)
    return _bot_username_cache


async def _open_menu(message: Message, state: FSMContext, _bot: Bot) -> None:
    """PV /help and `/start help` open the full public catalog for everyone."""
    user = message.from_user
    if user is None or user.is_bot:
        return
    await state.clear()
    role = await rbac.get_pv_role(user.id)
    await message.answer(
        ui.public_help_text(), reply_markup=pv_kb.public_help_kb(role)
    )


async def render_menu(cq: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Open the role-aware menu in place - also used by the PV dashboard."""
    await state.clear()
    await _ack(cq)
    user = cq.from_user
    if user is None:
        return
    role = await rbac.get_pv_role(user.id)
    await _edit(cq, menu_text(role, 0), menu_kb(role, 0, await _bot_username(bot)))


# ---------------------------------------------------------------- handlers ----
@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext, bot: Bot) -> None:
    if is_bot_message(message):
        return  # bots never trigger - and never get deleted by - the clean-chat protocol

    if message.chat.type == "private":
        await _open_menu(message, state, bot)
        return

    # clean chat protocol: the trigger vanishes on its own, the guide after 60s
    spawn(delete_later(message, DELETE_AFTER), name="help-trigger")

    username = await _bot_username(bot)
    rows: list[list[InlineKeyboardButton]] = []
    if username:
        rows.append(
            [
                InlineKeyboardButton(
                    text=PV_LABEL, url=f"https://t.me/{username}?start=help"
                )
            ]
        )
    if settings.support_url:
        rows.append([InlineKeyboardButton(text=SUPPORT_LABEL, url=settings.support_url)])
    markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    try:
        sent = await message.answer(guide_text(), reply_markup=markup)
    except Exception as exc:
        log.debug("help guide failed: %s", exc)
        return
    spawn(delete_later(sent, GUIDE_AFTER), name="help-reply")


@router.message(F.chat.type == "private", F.text.regexp(GROUP_TRIGGER))
async def deep_link_help(message: Message, state: FSMContext, bot: Bot) -> None:
    """The group button opens the full menu straight in the private chat."""
    await _open_menu(message, state, bot)


@router.callback_query(F.data.startswith(f"{NS}:"))
async def on_help_callback(cq: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    action, arg = resolve(cq.data)
    if not action:
        await _ack(cq)
        return

    if action == "close":
        await state.clear()
        await _ack(cq, "پنل راهنما بسته شد.")
        msg = cq.message
        if isinstance(msg, Message):
            try:
                await msg.delete()
            except Exception as exc:
                log.debug("help close failed: %s", exc)
        return

    user = cq.from_user
    if user is None:
        await _ack(cq)
        return
    role = await rbac.get_pv_role(user.id)

    if action == "pg":
        section = SECTIONS.get(arg)
        if section is None:
            await _ack(cq)
            return
        if role < section.min_role:
            await _ack(cq, "❌ دسترسی ندارید", alert=True)
            return
        await _ack(cq)
        await _edit(cq, section_text(section), page_kb(section, role, await _bot_username(bot)))
        return

    page = int(arg) if action == "idx" else 0
    await _ack(cq)
    await _edit(
        cq,
        menu_text(role, page),
        menu_kb(role, page, await _bot_username(bot)),
    )
