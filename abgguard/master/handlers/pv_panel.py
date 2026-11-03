from __future__ import annotations

import asyncio
import html
import logging
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command, Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ChatPermissions, InlineKeyboardButton, Message

from abgguard.config import settings
from abgguard.core import botpair, rbac, settings_store
from abgguard.core import groups as groups_store
from abgguard.core import redis as rk
from abgguard.core import trigger as trigger_store
from abgguard.core.rbac import Role
from abgguard.master import pv_keyboards as kb
from abgguard.master import ui
from abgguard.master.ephemeral import fa_num
from abgguard.master.handlers.help import render_menu
from abgguard.master.states.pv_states import PvAdminSG, PvGodSG
from abgguard.models.entities import BanLog, GroupSubscription
from abgguard.services import metrics, subscription
from abgguard.services.antiporn import porn_client
from abgguard.services.harvester import harvester

log = logging.getLogger("abgguard.master.pv")

# The PV routers are mounted BEFORE the group routers: a private-chat message
# never reaches the in-group panel, the text guards or the watchdog bot.
fsm_router = Router(name="master.pv-fsm")
router = Router(name="master.pv")

TRIGGER = "پنل"

# step 2: strict RBAC - the dashboard belongs to registered staff only
RBAC_REJECT = "⛔️ فقط ادمین‌ها و ساب‌ادمین‌های ثبت‌شده به پنل دسترسی دارند."

# fire-and-forget jobs (strong refs so they are never garbage collected)
_background: set[asyncio.Task] = set()


# --------------------------------------------------------------------- utils --
def _now() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S UTC")


def _esc(text: str | None) -> str:
    return html.escape(text or "")


async def _ack(cq: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
    try:
        await cq.answer(text, show_alert=alert)
    except Exception:  # already answered / query too old
        pass


async def _edit(cq: CallbackQuery, text: str, markup) -> None:
    """In-place update (project rule 6: never spam a new message per press)."""
    msg = cq.message
    if not isinstance(msg, Message) or msg.chat is None:
        return
    try:
        await msg.edit_text(text, reply_markup=markup)
    except Exception as exc:  # "message is not modified" and friends
        log.debug("pv edit skipped: %s", exc)


async def _edit_menu(bot: Bot, data: dict, text: str, markup) -> None:
    pv_chat, menu_id = data.get("pv_chat"), data.get("menu_msg")
    if not pv_chat or not menu_id:
        return
    try:
        await bot.edit_message_text(
            chat_id=int(pv_chat), message_id=int(menu_id), text=text, reply_markup=markup
        )
    except Exception as exc:
        log.debug("pv menu edit skipped: %s", exc)


async def _notice(message: Message, text: str) -> None:
    """FSM validation reply that self-destructs (rule 6: clean chat)."""
    try:
        sent = await message.answer(text)
    except Exception:
        return
    _spawn(_delete_later(sent), name="pv-notice")


async def _delete_later(message: Message) -> None:
    await asyncio.sleep(6)
    try:
        await message.delete()
    except Exception:
        pass


def _spawn(coro, *, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


async def _pv_ids(cq: CallbackQuery) -> tuple[int, int] | None:
    """(pv_chat, menu_msg) - only ever valid inside a private chat."""
    msg = cq.message
    if not isinstance(msg, Message) or msg.chat is None:
        return None
    if msg.chat.type != "private":
        return None
    return msg.chat.id, msg.message_id


async def _group_title(bot: Bot, chat_id: int) -> str:
    titles = await groups_store.known_titles([chat_id])
    if titles.get(chat_id):
        return titles[chat_id]
    try:
        chat = await bot.get_chat(chat_id)
    except Exception:
        return str(chat_id)
    title = chat.title or str(chat_id)
    await groups_store.upsert_group(chat_id, title=title, username=chat.username)
    return title


async def _managed(user_id: int) -> list[tuple[int, int, str]]:
    """[(chat_id, role, title)] for every group this user may configure."""
    bindings = await rbac.list_managed_chats(user_id)
    titles = await groups_store.known_titles([c for c, _ in bindings])
    return [(chat_id, role, titles.get(chat_id, str(chat_id))) for chat_id, role in bindings]


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


# ------------------------------------------------------------------- text ----
def _root_text(user, role: Role, groups: int) -> str:
    return (
        "👑 داشبورد مدیریت ABG Guard\n\n"
        f"🔰 سطح دسترسی: {role.label_fa} (Level {int(role)})\n"
        f"🆔 شناسه کاربری: <code>{user.id}</code>\n"
        f"🏘 گروه‌های تحت مدیریت: {groups}\n"
        f"🕐 {_now()}"
    )


async def _global_counts() -> dict[str, int]:
    r = rk.get_redis()
    actions = await BanLog.all().count()
    mutes = await BanLog.filter(reason__startswith="mute:").count()
    return {
        "groups": len(await groups_store.managed_chat_ids()),
        "members": int(await r.pfcount(rk.STATS_MEMBERS)),
        "actions": actions,
        "mutes": mutes,
        "bans": actions - mutes,
        "owners": len(await rbac.list_owner_bindings()),
    }


async def _status_text() -> str:
    snap = await metrics.system_snapshot()
    counts = await _global_counts()
    host, cg, pg = snap.get("host"), snap.get("cgroup"), snap.get("pg") or {}

    lines = ["📊 وضعیت کل سیستم", ""]
    cpu = snap.get("cpu_percent")
    lines.append(f"🖥 بار پردازنده: {'-' if cpu is None else cpu}%")
    if host:
        lines.append(
            f"🧠 حافظه میزبان: {metrics.fmt_mb(host['used'])} / "
            f"{metrics.fmt_mb(host['total'])} ({host['percent']}%)"
        )
    if cg:
        limit = f" / {metrics.fmt_mb(cg['limit'])}" if cg.get("limit") else ""
        pct = f" ({cg['percent']}%)" if cg.get("percent") is not None else ""
        lines.append(f"📦 حافظه کانتینر: {metrics.fmt_mb(cg['used'])}{limit}{pct}")

    containers = snap.get("containers")
    if containers:
        lines += ["", "🐳 وضعیت کانتینرها:"]
        for item in containers:
            cpu_txt = "-" if item.get("cpu") is None else f"{item['cpu']}%"
            lines.append(
                f"• {_esc(item['name'])}: {cpu_txt} | {metrics.fmt_mb(item.get('mem_used'))}"
            )

    lines.append("")
    if pg.get("connected"):
        lines.append(
            f"🛢 PostgreSQL: 🟢 متصل (open {pg.get('open', '-')} / max {pg.get('max', '-')})"
        )
    else:
        detail = pg.get("status")
        lines.append("🛢 PostgreSQL: 🔴 قطع" + (f" ({detail})" if detail else ""))

    redis_ms = snap.get("redis_ms")
    lines.append(
        f"🗄 Redis: {'🟢 ping ' + str(redis_ms) + ' ms' if redis_ms is not None else '🔴 خاموش'}"
    )
    lines += [
        "",
        "──────────────────────",
        f"🏘 گروه‌های مدیریت‌شده: {counts['groups']}",
        f"👑 مدیران گروه (Level 2): {counts['owners']}",
        f"👥 اعضای تحت محافظت: {counts['members']:,}",
        f"⚖️ کل اقدامات: {counts['actions']:,} "
        f"(بن: {counts['bans']:,} | میوت: {counts['mutes']:,})",
        "",
        f"🕐 {_now()}",
    ]
    return "\n".join(lines)


async def _ai_text(note: str | None = None) -> str:
    healthy = await porn_client.health()
    data = await asyncio.to_thread(metrics.dataset_stats)
    pct = round(data["size_mb"] / max(data["max_mb"], 0.1) * 100)
    lines = [
        "🟣 وضعیت هوش مصنوعی و دیتاست",
        "",
        (
            "🧠 سرویس NudeNet (pornscan): 🟢 سالم"
            if healthy
            else "🧠 سرویس NudeNet (pornscan): 🔴 در دسترس نیست"
        ),
        f"📚 بافر فعال: {data['size_mb']:.1f} MB / {data['max_mb']:.1f} MB ({pct}%)",
        f"🗜 آرشیو‌های ZIP: {data['archives']} عدد ({data['archive_mb']:.1f} MB)",
        f"📈 رکوردها: {data['records']:,} | چرخش‌ها: {data['rotations']} | "
        f"از دست رفته: {data['dropped']}",
        "",
        f"🕐 {_now()}",
    ]
    if note:
        lines.insert(2, f"\n✅ {note}")
    return "\n".join(lines)


async def _owners_rows() -> list[tuple[int, int, str]]:
    """One row per tracked group: (chat_id, owner_id, title).

    The list is built from the unified roster (Group + RoleBinding +
    GroupSubscription), so whitelisted groups can never disappear from the
    God panel just because their owner binding was created through another
    path.
    """
    chat_ids = sorted(await groups_store.managed_chat_ids())
    if not chat_ids:
        return []
    titles = await groups_store.known_titles(chat_ids)
    god = int(settings.god_admin_id)
    owners: dict[int, list[int]] = {}
    for chat_id, user_id in await rbac.list_owner_bindings():
        owners.setdefault(chat_id, []).append(user_id)
    rows: list[tuple[int, int, str]] = []
    for chat_id in chat_ids:
        specific = [u for u in owners.get(chat_id, []) if u != god]
        owner = specific[0] if specific else god
        rows.append((chat_id, owner, titles.get(chat_id, str(chat_id))))
    return rows


def _payment_methods_text(methods: list) -> str:
    lines = ["⚙️ مدیریت حساب‌ها و درگاه پرداخت", f"تعداد: {len(methods)}", ""]
    for m in methods[:20]:
        mark = "🟢 فعال" if m.is_active else "⚪️ غیرفعال"
        kind = "💳 کارت" if m.type == "card" else "🪙 ولت"
        lines.append(f"• {mark} | {kind} | {html.escape(m.title)}\n  <code>{html.escape(m.value)}</code>")
    if not methods:
        lines.append("هیچ روشی ثبت نشده؛ مقادیر پیش‌فرض از .env استفاده می‌شود.")
    lines += ["", "👆 روی هر مورد بزنید تا فعال/غیرفعال شود؛ 🗑 حذف می‌کند."]
    return "\n".join(lines)


async def _owners_text(note: str | None = None) -> str:
    rows = await _owners_rows()
    for chat_id, _owner, title in rows:
        await subscription.record_join(chat_id, title)
    chat_ids = [chat_id for chat_id, _owner, _title in rows]
    subs = {
        int(chat_id): (status, expires)
        for chat_id, status, expires in await GroupSubscription.filter(
            chat_id__in=chat_ids
        ).values_list("chat_id", "status", "expires_at")
    } if chat_ids else {}
    lines = ["👥 مدیران کل گروه‌ها (Level 2)", f"تعداد: {len(rows)}", ""]
    for chat_id, user_id, title in rows[:20]:
        status, expires = subs.get(
            chat_id, (GroupSubscription.UNPAID, None)
        )
        owner = (
            "God Admin"
            if int(user_id) == int(settings.god_admin_id)
            else f"کاربر <code>{user_id}</code>"
        )
        lines.append(
            f"• <b>{_esc(title)}</b>\n"
            f"  🆔 <code>{chat_id}</code> | 👑 {owner}\n"
            f"  📅 {ui.status_line(status, expires)}"
        )
    if not rows:
        lines.append("هنوز گروهی ثبت نشده است.")
    if note:
        lines += ["", f"✅ {note}"]
    lines += ["", f"🕐 {_now()}", "", "برای جزئیات، روی مورد نظر بزنید."]
    return "\n".join(lines)


def _group_panel_text(chat_id: int, role: Role, title: str, flags) -> str:
    lines = [
        "🛡 پنل تنظیم از راه دور",
        f"🏘 گروه: {_esc(title)}",
        f"🆔 شناسه گروه: <code>{chat_id}</code>",
        f"🔰 سطح دسترسی شما: {role.label_fa} (Level {int(role)})",
        "",
        "وضعیت قفل‌ها:",
    ]
    for field, emoji, label in kb.LOCK_TOGGLES:
        on = flags.is_on(field)
        lines.append(f"{emoji} {label}: {'🟢 فعال' if on else '🔴 غیرفعال'}")
    return "\n".join(lines)


def _badwords_text(title: str, words: list[str], note: str | None = None) -> str:
    lines = ["🤬 مدیریت کلمات ممنوعه", f"🏘 {_esc(title)}", f"تعداد: {len(words)}", ""]
    lines += [f"🔴 {w}" for w in words[:20]]
    if not words:
        lines.append("لیست سیاه خالی است.")
    if note:
        lines += ["", f"✅ {note}"]
    lines += ["", "کلمات جدید را با کاما بفرستید (بدون شلوغ‌کردن چت گروه)."]
    return "\n".join(lines)


def _subadmins_text(title: str, subs: list[int], note: str | None = None) -> str:
    lines = ["👥 مدیریت ساب‌ادمین‌ها", f"🏘 {_esc(title)}", f"تعداد: {len(subs)}", ""]
    lines += [f"• <code>{uid}</code>" for uid in subs[:20]]
    if not subs:
        lines.append("ساب‌ادمینی ثبت نشده است.")
    if note:
        lines += ["", f"✅ {note}"]
    return "\n".join(lines)


def _policy_text(title: str, policy, note: str | None = None) -> str:
    captcha = "🟢 کپچای عضویت: فعال" if policy.captcha_enabled else "🔴 کپچای عضویت: غیرفعال"
    lines = [
        "👋 پیام خوش‌آمد و کپچا",
        f"🏘 {_esc(title)}",
        "",
        captcha,
        f"⏱ مهلت تایید: {settings.captcha_timeout} ثانیه",
        "",
    ]
    if policy.welcome_text:
        lines += ["💬 پیام خوش‌آمد:", f"<blockquote>{_esc(policy.welcome_text)}</blockquote>"]
    else:
        lines.append("💬 پیام خوش‌آمد: تعریف نشده")
    if note:
        lines += ["", f"✅ {note}"]
    return "\n".join(lines)


async def _logs_text(chat_id: int, title: str) -> str:
    total = await BanLog.filter(chat_id=chat_id).count()
    rows = await BanLog.filter(chat_id=chat_id).order_by("-id").limit(10)
    lines = ["🟡 لاگ اقدامات اخیر", f"🏘 {_esc(title)}", f"کل اقدامات: {total}", ""]
    for row in rows:
        if str(row.reason).startswith("mute:"):
            icon, kind = "🟡", "میوت"
        else:
            icon, kind = "🔴", "بن"
        when = row.created_at.strftime("%m-%d %H:%M") if row.created_at else "-"
        lines.append(
            f"{icon} {kind} | کاربر <code>{row.user_id}</code> | {when} | {_esc(row.reason)}"
        )
    if not rows:
        lines.append("هنوز اقدامی ثبت نشده است.")
    lines += ["", f"🕐 {_now()}"]
    return "\n".join(lines)


async def _root_parts(bot: Bot, user, state: FSMContext) -> tuple[str, object]:
    await state.clear()
    role = await rbac.get_pv_role(user.id)
    groups = await rbac.list_managed_chats(user.id)
    text = _root_text(user, role, len(groups))
    if role >= Role.GOD:
        return text, kb.god_root_kb(has_groups=bool(groups))
    if role >= Role.SUBADMIN:
        return text, kb.admin_root_kb(await _bot_username(bot), bool(groups))
    return (
        text + "\n\nشما کاربر عادی هستید؛ امکانات مدیریتی نمایش داده نمی‌شود.",
        kb.user_root_kb(await _bot_username(bot)),
    )


# ------------------------------------------------------------- entry points --
# ------------------------------------------------------ in-bot receipt flow --
class ReceiptWait(Filter):
    """True only while this user has an open `sub:receipt` submission window.

    Kept as a filter (not a router-level switch) so ordinary PV traffic -
    /start, «پنل», FSM steps - flows exactly as before.
    """

    async def __call__(self, message: Message) -> bool:
        user = message.from_user
        if user is None:
            return False
        try:
            return bool(await rk.get_redis().get(f"{rk.P}:receipt_wait:{user.id}"))
        except Exception:
            return False


@router.message(F.chat.type == "private", ReceiptWait())
async def on_receipt_sent(message: Message, bot: Bot) -> None:
    """The receipt itself (photo/document/transaction text) - straight to God.

    The source message is copied verbatim to the God Admin's PV with one-click
    ✅/❌ buttons; nothing ever leaves the bot (directive 2).
    """
    user = message.from_user
    if user is None:
        return
    raw = await rk.get_redis().get(f"{rk.P}:receipt_wait:{user.id}")
    if not raw:
        return
    chat_id = int(raw)
    await rk.get_redis().delete(f"{rk.P}:receipt_wait:{user.id}")
    titles = await groups_store.known_titles([chat_id])
    title = titles.get(chat_id, str(chat_id))
    uname = f"@{user.username}" if user.username else str(user.id)
    await message.answer(
        "✅ فیش شما ثبت و برای مدیریت ارسال شد. "
        "پس از تایید، اشتراک ۳۰ روزه گروه فعال می‌گردد."
    )
    try:
        await bot.send_message(
            settings.god_admin_id,
            f"🧾 <b>فیش پرداخت جدید</b>\n"
            f"🏘 گروه: <b>{title}</b>\n"
            f"🆔 <code>{chat_id}</code>\n"
            f"👤 از: <a href=\"tg://user?id={user.id}\">{user.id}</a> ({uname})",
        )
        await bot.copy_message(
            chat_id=settings.god_admin_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=ui.markup(
                [
                    InlineKeyboardButton(
                        text="✅ تایید و شارژ ۳۰ روزه",
                        callback_data=f"sub:approve:{chat_id}:{user.id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ رد فیش",
                        callback_data=f"sub:reject:{chat_id}:{user.id}",
                    )
                ],
            ),
        )
    except Exception:
        log.warning("receipt forwarding failed chat=%s user=%s", chat_id, user.id)


async def _maybe_pay_card(message: Message, bot: Bot) -> bool:
    """`/start pay_{chat_id}` / `/start sub_{chat_id}` deep-links -> the card."""
    user = message.from_user
    raw = (message.text or "").strip()
    parts = raw.split(maxsplit=1)
    if len(parts) < 2 or parts[0].split("@", 1)[0].lower() != "/start":
        return False
    arg = parts[1].strip()
    head, _, raw_id = arg.partition("_")
    raw_id = raw_id.split("@", 1)[0]
    if not (head in {"pay", "sub"} and raw_id.lstrip("-").isdigit()):
        return False

    chat_id = int(raw_id)
    title = str(chat_id)
    try:
        chat = await bot.get_chat(chat_id)
        title = chat.title or title
    except Exception:
        pass
    await groups_store.upsert_group(chat_id, title=title)
    row = await subscription.record_join(chat_id, title)
    await message.answer(
        ui.pay_screen_text(chat_id, title, row.status, row.expires_at),
        reply_markup=await ui.pay_screen_kb(chat_id),
    )
    log.info("pay deep-link chat=%s user=%s", chat_id, user.id if user else 0)
    return True


async def _public_start_parts(user) -> tuple[str, object]:
    role = await rbac.get_pv_role(user.id)
    return ui.public_start_text(), kb.public_start_kb(role)


async def _public_help_parts(user) -> tuple[str, object]:
    role = await rbac.get_pv_role(user.id)
    return ui.public_help_text(), kb.public_help_kb(role)


def _is_public_start(message: Message) -> tuple[bool, str]:
    raw = (message.text or "").strip()
    parts = raw.split(maxsplit=1)
    head = parts[0].split("@", 1)[0].lower() if parts else ""
    return head == "/start", (parts[1].strip() if len(parts) > 1 else "")


async def _open_pv(message: Message, state: FSMContext, bot: Bot) -> None:
    user = message.from_user
    if user is None or user.is_bot:
        return
    if await _maybe_pay_card(message, bot):
        return

    is_start, arg = _is_public_start(message)
    if is_start:
        await state.clear()
        text, markup = (
            await _public_help_parts(user)
            if arg.split("@", 1)[0].lower() == "help"
            else await _public_start_parts(user)
        )
        await message.answer(text, reply_markup=markup)
        return

    if await rbac.get_pv_role(user.id) < Role.SUBADMIN:
        return  # the settings panel itself stays restricted to registered staff
    text, markup = await _root_parts(bot, user, state)
    await message.answer(text, reply_markup=markup)


@router.message(F.chat.type == "private", Command("start", "panel"))
async def pv_command(message: Message, state: FSMContext, bot: Bot) -> None:
    await _open_pv(message, state, bot)


@router.message(F.chat.type == "private", F.text.regexp(rf"^\s*{TRIGGER}\s*$"))
async def pv_trigger(message: Message, state: FSMContext, bot: Bot) -> None:
    await _open_pv(message, state, bot)


@router.message(
    F.chat.type == "private", F.text, F.text.func(lambda t: not t.startswith("/"))
)
async def pv_untagged(message: Message, state: FSMContext, bot: Bot) -> None:
    """Plain text in PV: open the dashboard instead of staying mute."""
    if await state.get_state() is not None:
        return  # a foreign (in-group) FSM state leaked into PV - ignore it
    await _open_pv(message, state, bot)


# ---------------------------------------------------------------- navigation --
@router.callback_query(F.data == "pv:close")
async def pv_close(cq: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _ack(cq)
    msg = cq.message
    if isinstance(msg, Message):
        try:
            await msg.delete()
        except Exception as exc:
            log.debug("pv close failed: %s", exc)


@router.callback_query(F.data.in_({"pv:root", "pv:home"}))
async def pv_root(cq: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    user = cq.from_user
    if user is None:
        await _ack(cq)
        return
    if await rbac.get_pv_role(user.id) < Role.SUBADMIN:
        await _ack(cq, RBAC_REJECT, alert=True)
        return
    text, markup = await _root_parts(bot, user, state)
    await _ack(cq)
    await _edit(cq, text, markup)


# ------------------------------------------------------- public PV catalog --
@router.callback_query(F.data == "pv:public:start")
async def pv_public_start(cq: CallbackQuery, state: FSMContext) -> None:
    user = cq.from_user
    if user is None or await _pv_ids(cq) is None:
        await _ack(cq)
        return
    await state.clear()
    await _ack(cq)
    text, markup = await _public_start_parts(user)
    await _edit(cq, text, markup)


@router.callback_query(F.data == "pv:public:help")
async def pv_public_help(cq: CallbackQuery, state: FSMContext) -> None:
    user = cq.from_user
    if user is None or await _pv_ids(cq) is None:
        await _ack(cq)
        return
    await state.clear()
    await _ack(cq)
    text, markup = await _public_help_parts(user)
    await _edit(cq, text, markup)


@router.callback_query(F.data == "pv:public:mygroups")
async def pv_public_mygroups(cq: CallbackQuery, state: FSMContext) -> None:
    user = cq.from_user
    if user is None or await _pv_ids(cq) is None:
        await _ack(cq)
        return
    await state.clear()
    await _ack(cq)
    role = await rbac.get_pv_role(user.id)
    rows = await _managed(user.id)
    entries = [(chat_id, title) for chat_id, _role, title in rows]
    await _edit(
        cq,
        ui.public_mygroups_text(entries),
        kb.public_mygroups_kb(entries, role),
    )


@router.callback_query(F.data.startswith("pv:public:pay:"))
async def pv_public_pay(cq: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    user = cq.from_user
    if user is None or await _pv_ids(cq) is None:
        await _ack(cq)
        return
    parts = (cq.data or "").split(":")
    try:
        chat_id = int(parts[3].split("@", 1)[0])
    except (IndexError, ValueError):
        await _ack(cq)
        return
    if await rbac.role_in(chat_id, user.id) < Role.SUBADMIN:
        await _ack(cq, RBAC_REJECT, alert=True)
        return
    await state.clear()
    await _ack(cq)
    title = await _group_title(bot, chat_id)
    row = await subscription.record_join(chat_id, title)
    await _edit(
        cq,
        ui.pay_screen_text(chat_id, title, row.status, row.expires_at),
        await ui.pay_screen_kb(chat_id, back="pv:public:mygroups"),
    )


@router.callback_query(F.data == "pv:help")
async def pv_help(cq: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Dashboard "help" -> the full role-aware, paginated help menu."""
    user = cq.from_user
    if user is None:
        await _ack(cq)
        return
    if await rbac.get_pv_role(user.id) < Role.SUBADMIN:
        await _ack(cq, RBAC_REJECT, alert=True)
        return
    await render_menu(cq, state, bot)


# ======================================================= SUBSCRIPTION (in-panel)
@router.callback_query(F.data == "pv:substat")
async def pv_substat(cq: CallbackQuery, state: FSMContext) -> None:
    """Main-panel "Subscription & Renewal" -> per-group status list."""
    user = cq.from_user
    ids = await _pv_ids(cq)
    if user is None or ids is None:
        await _ack(cq)
        return
    role = await rbac.get_pv_role(user.id)
    if role < Role.SUBADMIN:
        await _ack(cq, RBAC_REJECT, alert=True)
        return
    await state.clear()
    await _ack(cq)
    if role >= Role.GOD:
        all_ids = await groups_store.managed_chat_ids()
        titles = await groups_store.known_titles(all_ids)
        entries = [(c, Role.GOD, titles.get(c, str(c)) or str(c)) for c in all_ids]
    else:
        entries = await _managed(user.id)
    lines = ["💳 وضعیت اشتراک و تمدید", ""]
    rows: list[list[InlineKeyboardButton]] = []
    for chat_id, _role, title in entries[:25]:
        row = await subscription.record_join(chat_id, title)
        lines.append(f"• {html.escape(title)}: {ui.status_line(row.status, row.expires_at)}")
        rows.append([
            InlineKeyboardButton(text=f"🛡 {title[:40]}"[:64], callback_data=f"pv:substat:{chat_id}")
        ])
    if not entries:
        lines.append("هنوز گروهی ثبت نشده است.")
    await _edit(cq, "\n".join(lines), ui.markup(*rows, [ui.support_btn()]))


@router.callback_query(F.data.startswith("pv:substat:"))
async def pv_substat_chat(cq: CallbackQuery, state: FSMContext) -> None:
    user = cq.from_user
    ids = await _pv_ids(cq)
    if user is None or ids is None:
        await _ack(cq)
        return
    parts = (cq.data or "").split(":")
    chat_id = int(parts[2].split("@", 1)[0]) if len(parts) > 2 else 0
    if not chat_id or await rbac.role_in(chat_id, user.id) < Role.SUBADMIN:
        await _ack(cq, RBAC_REJECT, alert=True)
        return
    await state.clear()
    await _ack(cq)
    titles = await groups_store.known_titles([chat_id])
    title = titles.get(chat_id, str(chat_id))
    row = await subscription.record_join(chat_id, title)
    kb = ui.subscribe_kb(chat_id, paid=ui.is_premium(row.status, row.expires_at))
    await _edit(cq, ui.pay_view_text(chat_id, title, row.status, row.expires_at), kb)


# ============================================================== GOD (Level 3) ==
@router.callback_query(F.data.startswith("pv:god:"))
async def pv_god_callback(cq: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    user = cq.from_user
    ids = await _pv_ids(cq)
    if user is None or ids is None:
        await _ack(cq)
        return
    if not await rbac.is_god(user.id):
        await _ack(cq, RBAC_REJECT, alert=True)
        return

    pv_chat, menu_id = ids
    parts = (cq.data or "").split(":")
    action = parts[2] if len(parts) > 2 else ""
    await state.clear()

    if action in {"home", "root"}:
        await _ack(cq)
        text, markup = await _root_parts(bot, user, state)
        await _edit(cq, text, markup)
        return

    # ---- 1. global system status ------------------------------------------
    if action == "sys":
        await _ack(cq)
        await _edit(cq, await _status_text(), kb.god_status_kb())
        return

    # ---- 2. AI / ML harvester ---------------------------------------------
    if action == "ai":
        await _ack(cq)
        await _edit(cq, await _ai_text(), kb.god_ai_kb())
        return

    if action == "flush":
        await _ack(cq, "در حال فشرده‌سازی...")
        done = await harvester.rotate_now()
        note = "فایل فعال به ZIP تبدیل شد." if done else "بافر خالی است؛ چیزی تخلیه نشد."
        await _edit(cq, await _ai_text(note), kb.god_ai_kb())
        return

    # ---- 3. group owners manager ------------------------------------------
    if action == "owners":
        await _ack(cq)
        await _edit(cq, await _owners_text(), kb.god_owners_kb(await _owners_rows()))
        return

    # ---- payments gateway management (directive #2) -----------------------
    if action == "pay":
        part3 = parts[3] if len(parts) > 3 else ""
        if part3 == "tgl" and len(parts) > 4:
            await settings_store.payment_toggle(int(parts[4]))
            await _ack(cq, "تغییر ذخیره شد ✅")
        elif part3 == "del" and len(parts) > 4:
            await settings_store.payment_delete(int(parts[4]))
            await _ack(cq, "روش پرداخت حذف شد ✅")
        elif part3 == "add":
            await state.set_state(PvGodSG.pay_title)
            await state.update_data(pv_chat=pv_chat, menu_msg=menu_id)
            await _ack(cq)
            await _edit(
                cq,
                "⚙️ افزودن حساب/درگاه پرداخت\n\n"
                "نوع را انتخاب کنید:",
                kb.god_payment_add_kb(),
            )
            return
        elif part3 == "pt" and len(parts) > 4 and parts[4] in {"card", "crypto"}:
            await state.set_state(PvGodSG.pay_title)
            await state.update_data(
                pv_chat=pv_chat, menu_msg=menu_id, pay_type=parts[4]
            )
            await _ack(cq)
            await _edit(
                cq,
                "💳 مرحله ۱ از ۲ — عنوان روش پرداخت را بفرستید\n"
                "مثال: <code>بانک ملت</code> یا <code>Tether TRC20</code>",
                kb.god_payment_post_kb(),
            )
            return
        else:
            await _ack(cq)
        methods = await settings_store.payment_list(active_only=False)
        await _edit(cq, _payment_methods_text(methods), kb.god_payment_kb(methods))
        return

    # ---- SaaS: manual whitelist / manual 30-day activation ----------------
    if action == "sub" and len(parts) > 4 and parts[3] in {"man", "pay"}:
        try:
            chat_id = int(parts[4])
        except ValueError:
            await _ack(cq)
            return
        title = (await groups_store.known_titles([chat_id])).get(chat_id, str(chat_id))
        await groups_store.upsert_group(chat_id, title=title)
        await groups_store.ensure_owner(chat_id)
        if parts[3] == "man":
            await subscription.manual_whitelist(chat_id, title)
            row = await subscription.record_join(chat_id)
            status = GroupSubscription.MANUAL_WHITELIST
            note = "🟣 گروه به صورت دستی ثبت شد؛ هرگز درخواست پرداخت نمی‌شود."
        else:
            await subscription.activate_paid(chat_id)
            row = await subscription.record_join(chat_id)
            status = GroupSubscription.ACTIVE
            note = "🟢 اشتراک ۳۰ روزه به طور دستی فعال شد."
        await _ack(cq, "ذخیره شد ✅")
        await _edit(
            cq,
            f"💳 مدیریت دستی اشتراک گروه\n\n"
            f"🏘 گروه: <code>{chat_id}</code>\n"
            f"📅 وضعیت: {ui.status_line(status, row.expires_at)}\n\n{note}",
            ui.markup([ui.btn_cb("[🔙 بازگشت | Back]", "pv:god:owners")]),
        )
        return

    if action == "ow" and len(parts) > 3 and parts[3] == "add":
        await state.set_state(PvGodSG.owner_chat)
        await state.update_data(pv_chat=pv_chat, menu_msg=menu_id)
        await _ack(cq)
        await _edit(
            cq,
            "➕ افزودن مدیر گروه — مرحله ۱ از ۳\n\n"
            "شناسه عددی گروه (chat_id) را بفرستید:\n"
            "<code>-1001234567890</code>\n\n"
            "نیازی به حضور شما در گروه نیست.\n"
            "برای انصراف دکمه بازگشت را بزنید.",
            kb.god_owner_step_kb(),
        )
        return

    # step 3/3 - grant the level for an explicit (user_id, chat_id) pair.
    # The ids travel inside the callback data, so the grant never depends on
    # a stale FSM state (rule 3: the composite key is always explicit).
    if action == "ow" and len(parts) > 6 and parts[3] == "lvl":
        level, raw_user, raw_chat = parts[4], parts[5], parts[6]
        if level not in {"1", "2"} or not (
            raw_user.isdigit() and raw_chat.lstrip("-").isdigit()
        ):
            await _ack(cq, "❌ درخواست نامعتبر است", alert=True)
            return
        target_user, target_chat = int(raw_user), int(raw_chat)
        try:
            if level == "2":
                await rbac.add_admin(target_chat, target_user, granted_by=user.id)
                await groups_store.ensure_owner(target_chat, target_user)
                note = (
                    f"کاربر {target_user} به عنوان ادمین گروه (Level 2) "
                    f"گروه {target_chat} ثبت شد."
                )
            else:
                await rbac.add_subadmin(target_chat, target_user, granted_by=user.id)
                note = (
                    f"کاربر {target_user} به عنوان ساب‌ادمین (Level 1) "
                    f"گروه {target_chat} ثبت شد."
                )
        except (PermissionError, ValueError) as exc:
            await _ack(cq, f"❌ {html.escape(str(exc))}", alert=True)
            return
        await groups_store.upsert_group(target_chat)
        await _ack(cq, "ثبت شد ✅")
        await _edit(
            cq, await _owners_text(note), kb.god_owners_kb(await _owners_rows())
        )
        return

    if action == "ow" and len(parts) > 4 and parts[3] == "new":
        try:
            chat_id = int(parts[4])
        except ValueError:
            await _ack(cq)
            return
        title = (await groups_store.known_titles([chat_id])).get(chat_id, str(chat_id))
        await state.set_state(PvGodSG.owner_user)
        await state.update_data(
            pv_chat=pv_chat, menu_msg=menu_id, target_chat=chat_id
        )
        await _ack(cq)
        await _edit(
            cq,
            "👤 تغییر مالک گروه\n\n"
            f"🏘 گروه: {_esc(title)}\n"
            f"🆔 <code>{chat_id}</code>\n\n"
            "شناسه عددی مالک جدید (Level 2) را بفرستید:\n"
            "<code>123456789</code>",
            kb.god_owner_step_kb(),
        )
        return

    if action == "ow" and len(parts) > 4 and parts[3] == "rm":
        try:
            chat_id = int(parts[4])
        except ValueError:
            await _ack(cq)
            return
        await groups_store.remove_chat(chat_id)
        await rk.presence_remove(chat_id)
        try:
            await bot.leave_chat(chat_id)
        except Exception:
            log.debug("master leave after remove failed chat=%s", chat_id, exc_info=True)
        await botpair.worker_leave(chat_id)
        await _ack(cq, "گروه حذف شد ✅")
        await _edit(
            cq,
            await _owners_text("گروه به طور کامل حذف شد."),
            kb.god_owners_kb(await _owners_rows()),
        )
        return

    if action == "ow" and len(parts) > 5 and parts[3] == "del":
        try:
            target, chat_id = int(parts[4]), int(parts[5])
        except ValueError:
            await _ack(cq)
            return
        try:
            await rbac.remove_admin(chat_id, target, requested_by=user.id)
        except PermissionError:
            await _ack(cq, "❌ مجوز ندارید", alert=True)
            return
        # the last owner being unbound revokes the roster entry entirely: the
        # periodic audit then orders both bots to leave that orphaned group.
        # Groups holding an actual paid / trial subscription keep their record.
        if not await rbac.list_owner_ids(chat_id):
            sub = await subscription.record_join(chat_id)
            if sub.status in {
                GroupSubscription.UNPAID,
                GroupSubscription.EXPIRED,
                GroupSubscription.MANUAL_WHITELIST,
            }:
                await groups_store.remove_chat(chat_id)
        await _ack(cq, "دسترسی لغو شد")
        await _edit(
            cq,
            await _owners_text("دسترسی مدیر گروه لغو شد."),
            kb.god_owners_kb(await _owners_rows()),
        )
        return

    if action == "ow" and len(parts) > 4:
        try:
            target, chat_id = int(parts[3]), int(parts[4])
        except ValueError:
            await _ack(cq)
            return
        title = (await groups_store.known_titles([chat_id])).get(chat_id, str(chat_id))
        sub = await subscription.record_join(chat_id)
        owner = "God Admin" if target == int(settings.god_admin_id) else f"<code>{target}</code>"
        await _ack(cq)
        await _edit(
            cq,
            "👤 جزئیات مدیر گروه\n\n"
            f"🆔 کاربر: {owner}\n"
            f"🏘 گروه: {_esc(title)}\n"
            f"🆔 شناسه گروه: <code>{chat_id}</code>\n"
            f"📅 وضعیت اشتراک: {ui.status_line(sub.status, sub.expires_at)}\n"
            f"🎁 تست رایگان: {'استفاده شد' if sub.trial_used else 'در دسترس'}",
            kb.owner_detail_kb(target, chat_id),
        )
        return

    # ---- 4. global broadcast ----------------------------------------------
    if action == "bcast":
        targets = sorted(await groups_store.managed_chat_ids())
        if not targets:
            await _ack(cq, "گروهی برای ارسال ثبت نشده است", alert=True)
            return
        await state.set_state(PvGodSG.broadcast)
        await state.update_data(pv_chat=pv_chat, menu_msg=menu_id)
        await _ack(cq)
        await _edit(
            cq,
            "📢 اطلاع‌رسانی همگانی\n\n"
            f"تعداد گروه‌های هدف: {len(targets)}\n\n"
            "متن پیام را همین‌جا بفرستید تا برای همه گروه‌ها ارسال شود.\n"
            "برای انصراف دکمه بازگشت را بزنید.",
            kb.god_broadcast_kb(),
        )
        return

    await _ack(cq)


# ========================================================= ADMIN (Level 2/1) ===
async def _require_group(cq: CallbackQuery, chat_id: int, min_role: Role) -> Role | None:
    """RBAC re-checked from PostgreSQL on every privileged action (rule 3)."""
    user = cq.from_user
    if user is None:
        await _ack(cq)
        return None
    role = await rbac.role_in(chat_id, user.id)
    if role < min_role:
        await _ack(cq, RBAC_REJECT, alert=True)
        return None
    return role


async def _render_group_panel(cq: CallbackQuery, bot: Bot, chat_id: int, role: Role) -> None:
    flags = await settings_store.get_flags(chat_id)
    title = await _group_title(bot, chat_id)
    trigger = await trigger_store.get_trigger(chat_id)
    await _edit(
        cq,
        _group_panel_text(chat_id, role, title, flags),
        kb.group_panel_kb(chat_id, flags, role, trigger),
    )


@router.callback_query(F.data.startswith("pv:admin:"))
async def pv_admin_callback(cq: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    user = cq.from_user
    ids = await _pv_ids(cq)
    if user is None or ids is None:
        await _ack(cq)
        return

    parts = (cq.data or "").split(":")
    action = parts[2] if len(parts) > 2 else ""
    await state.clear()

    # ---- group selector ----------------------------------------------------
    if action == "groups":
        rows = await _managed(user.id)
        await _ack(cq)
        if not rows:
            await _edit(
                cq,
                "🏢 هنوز گروهی به شما اختصاص داده نشده است.\n\n"
                "گاد ادمین (Level 3) می‌تواند شما را با شناسه عددی به گروه‌ای معرفی کند.",
                kb.admin_root_kb(await _bot_username(bot), has_groups=False),
            )
            return
        await _edit(
            cq,
            "🏢 انتخاب گروه\n"
            f"تعداد: {len(rows)}\n\n"
            "گروه مورد نظر را برای تنظیم از راه دور انتخاب کنید:",
            kb.group_list_kb(rows),
        )
        return

    # ---- selected group panel ----------------------------------------------
    if action == "g" and len(parts) > 3:
        chat_id = _int_or_none(parts[3])
        if chat_id is None:
            await _ack(cq)
            return
        role = await _require_group(cq, chat_id, Role.SUBADMIN)
        if role is None:
            return
        await _ack(cq)
        await _render_group_panel(cq, bot, chat_id, role)
        return

    if action == "lock" and len(parts) > 4:
        chat_id = _int_or_none(parts[3])
        name = settings_store.INV_RKEY.get(parts[4]) if chat_id is not None else None
        if chat_id is None or name is None:
            await _ack(cq)
            return
        role = await _require_group(cq, chat_id, Role.SUBADMIN)
        if role is None:
            return
        await settings_store.toggle(chat_id, name)
        await _ack(cq, "تغییر ذخیره شد ✅")
        await _render_group_panel(cq, bot, chat_id, role)
        return

    # ---- custom trigger name (Level 1/2/3) --------------------------------
    if action == "trg" and len(parts) > 3:
        chat_id = _int_or_none(parts[3])
        if chat_id is None:
            await _ack(cq)
            return
        role = await _require_group(cq, chat_id, Role.SUBADMIN)
        if role is None:
            return
        current = await trigger_store.get_trigger(chat_id)
        title = await _group_title(bot, chat_id)
        await state.set_state(PvAdminSG.trigger_set)
        await state.update_data(target=chat_id, pv_chat=ids[0], menu_msg=ids[1])
        await _ack(cq)
        await _edit(
            cq,
            "🏷 نام فراخوانی ربات\n\n"
            f"نام فعلی: <code>{_esc(current)}</code>\n\n"
            "نام جدید را همین‌جا بفرستید "
            f"({fa_num(trigger_store.TRIGGER_MIN)} تا "
            f"{fa_num(trigger_store.TRIGGER_MAX)} کاراکتر):\n\n"
            f"گروه: {_esc(title)}",
            kb.trigger_input_kb(chat_id),
        )
        return

    # ---- blacklist ----------------------------------------------------------
    if action == "w" and len(parts) > 3:
        await _badwords_route(cq, state, bot, parts, ids)
        return

    # ---- sub-admins (Level 2 only) ------------------------------------------
    if action == "sa" and len(parts) > 3:
        await _subadmins_route(cq, state, bot, parts, user.id, ids)
        return

    # ---- welcome & captcha ---------------------------------------------------
    if action == "pol" and len(parts) > 3:
        chat_id = _int_or_none(parts[3])
        if chat_id is None:
            await _ack(cq)
            return
        role = await _require_group(cq, chat_id, Role.SUBADMIN)
        if role is None:
            return
        await _ack(cq)
        title = await _group_title(bot, chat_id)
        policy = await settings_store.get_policy(chat_id)
        await _edit(cq, _policy_text(title, policy), kb.policy_kb(chat_id, policy))
        return

    if action == "cap" and len(parts) > 3:
        chat_id = _int_or_none(parts[3])
        if chat_id is None:
            await _ack(cq)
            return
        role = await _require_group(cq, chat_id, Role.SUBADMIN)
        if role is None:
            return
        policy = await settings_store.get_policy(chat_id)
        await settings_store.set_captcha(chat_id, not policy.captcha_enabled)
        policy = await settings_store.get_policy(chat_id)
        await _ack(cq, "ذخیره شد ✅")
        title = await _group_title(bot, chat_id)
        await _edit(cq, _policy_text(title, policy), kb.policy_kb(chat_id, policy))
        return

    if action == "wel" and len(parts) > 3:
        await _welcome_route(cq, state, bot, parts)
        return

    # ---- remote logs ---------------------------------------------------------
    if action == "logs" and len(parts) > 3:
        chat_id = _int_or_none(parts[3])
        if chat_id is None:
            await _ack(cq)
            return
        role = await _require_group(cq, chat_id, Role.SUBADMIN)
        if role is None:
            return
        await _ack(cq)
        title = await _group_title(bot, chat_id)
        await _edit(cq, await _logs_text(chat_id, title), kb.logs_kb(chat_id))
        return

    await _ack(cq)


@router.callback_query(F.data.startswith("modact:"))
async def modact_callback(cq: CallbackQuery, bot: Bot) -> None:
    """Handle inline action buttons from PV ModLog cards.

    modact:unmute:<chat_id>:<user_id>  -> fully unmute the user
    modact:ban:<chat_id>:<user_id>     -> ban the user
    modact:dismiss                     -> remove the keyboard / delete the card
    """
    user = cq.from_user
    if user is None:
        await cq.answer()
        return

    parts = (cq.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "unmute" and len(parts) == 4:
        chat_id = int(parts[2])
        target_user_id = int(parts[3])
        # Only Level 2+ may unmute via the card.
        role = await rbac.get_role(chat_id, user.id)
        if role is None or role < Role.ADMIN:
            await cq.answer("فقط مالک گروه می‌تواند سکوت را لغو کند.", alert=True)
            return
        full = ChatPermissions(
            can_send_messages=True,
            can_send_audios=True,
            can_send_documents=True,
            can_send_photos=True,
            can_send_videos=True,
            can_send_video_notes=True,
            can_send_voice_notes=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
            can_change_info=False,
            can_invite_users=False,
            can_pin_messages=False,
        )
        try:
            await bot.restrict_chat_member(chat_id, target_user_id, permissions=full)
            new_text = (
                (cq.message.text or "")
                + "\n\nوضعیت جدید: 🔊 کاربر توسط ادمین لغو سکوت شد."
            )
            await cq.message.edit_text(new_text, parse_mode="HTML")
            await cq.message.edit_reply_markup(reply_markup=None)
            await cq.answer("سکوت کاربر لغو شد ✅")
        except Exception as exc:
            log.warning("modact unmute failed chat=%s user=%s: %s", chat_id, target_user_id, exc)
            await cq.answer("خطا در لغو سکوت", alert=True)
        return

    if action == "ban" and len(parts) == 4:
        chat_id = int(parts[2])
        target_user_id = int(parts[3])
        role = await rbac.get_role(chat_id, user.id)
        if role is None or role < Role.ADMIN:
            await cq.answer("فقط مالک گروه می‌تواند کاربر را بن کند.", alert=True)
            return
        try:
            await bot.ban_chat_member(chat_id, target_user_id)
            new_text = (
                (cq.message.text or "")
                + "\n\nوضعیت جدید: ⛔️ کاربر توسط ادمین بن/اخراج شد."
            )
            await cq.message.edit_text(new_text, parse_mode="HTML")
            await cq.message.edit_reply_markup(reply_markup=None)
            await cq.answer("کاربر بن شد ⛔️")
        except Exception as exc:
            log.warning("modact ban failed chat=%s user=%s: %s", chat_id, target_user_id, exc)
            await cq.answer("خطا در بن کردن کاربر", alert=True)
        return

    if action == "dismiss":
        try:
            await cq.message.edit_reply_markup(reply_markup=None)
            await cq.answer("نادیده گرفته شد 🗑")
        except Exception:
            await cq.answer()
        return

    await cq.answer()


def _int_or_none(raw: str | None) -> int | None:
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


async def _badwords_route(
    cq: CallbackQuery, state: FSMContext, bot: Bot, parts: list[str], ids: tuple[int, int]
) -> None:
    # pv:admin:w:<chat>            -> list
    # pv:admin:w:add:<chat>        -> prompt (FSM)
    # pv:admin:w:del:<chat>:<word> -> delete one
    # pv:admin:w:clr:<chat>        -> confirm clear
    # pv:admin:w:do:<chat>         -> perform clear
    if len(parts) == 4:
        sub, chat_id = "", _int_or_none(parts[3])
    elif len(parts) > 4:
        sub, chat_id = parts[3], _int_or_none(parts[4])
    else:
        sub, chat_id = "", None
    if chat_id is None:
        await _ack(cq)
        return

    role = await _require_group(cq, chat_id, Role.SUBADMIN)
    if role is None:
        return

    words = sorted(await settings_store.load_badwords(chat_id))
    title = await _group_title(bot, chat_id)

    if sub == "add":
        await state.set_state(PvAdminSG.badword_add)
        await state.update_data(target=chat_id, pv_chat=ids[0], menu_msg=ids[1])
        await _ack(cq)
        await _edit(
            cq,
            _badwords_text(title, words, "کلمات جدید را با کاما بفرستید:"),
            kb.badwords_input_kb(chat_id),
        )
        return

    if sub == "del" and len(parts) > 5:
        raw = ":".join(parts[5:])[:kb.WORD_BUDGET]
        target = raw if raw in words else ""
        if not target:
            cands = [w for w in words if w.startswith(raw)]
            if len(cands) == 1:
                target = cands[0]
            elif len(cands) > 1:
                await _ack(cq, "کلمه نامشخص است؛ دوباره از لیست انتخاب کنید", alert=True)
                return
        if not target:
            await _ack(cq, "این کلمه دیگر در لیست نیست", alert=True)
            return
        await settings_store.remove_badword(chat_id, target)
        words = sorted(await settings_store.load_badwords(chat_id))
        await _ack(cq, "حذف شد")
        await _edit(
            cq,
            _badwords_text(title, words, f"«{target}» حذف شد."),
            kb.badwords_kb(chat_id, words),
        )
        return

    if sub == "clr":
        await _ack(cq)
        await _edit(
            cq,
            f"⚠️ پاک‌کردن کل لیست سیاه\n🏘 {_esc(title)}\nتعداد: {len(words)}\n\n"
            "همه کلمات حذف شوند؟",
            kb.confirm_clear_badwords_kb(chat_id),
        )
        return

    if sub == "do":
        for word in words:
            await settings_store.remove_badword(chat_id, word)
        await _ack(cq, "لیست پاک شد")
        await _edit(
            cq, _badwords_text(title, [], "لیست سیاه پاک شد."), kb.badwords_kb(chat_id, [])
        )
        return

    await _ack(cq)
    await _edit(cq, _badwords_text(title, words), kb.badwords_kb(chat_id, words))


async def _subadmins_route(
    cq: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    parts: list[str],
    actor: int,
    ids: tuple[int, int],
) -> None:
    # pv:admin:sa:<chat> | pv:admin:sa:add:<chat> | pv:admin:sa:del:<chat>:<uid>
    if len(parts) == 4:
        sub, chat_id = "", _int_or_none(parts[3])
    elif len(parts) > 4:
        sub, chat_id = parts[3], _int_or_none(parts[4])
    else:
        sub, chat_id = "", None
    if chat_id is None:
        await _ack(cq)
        return

    role = await _require_group(cq, chat_id, Role.ADMIN)  # Level 2 + Level 3 only
    if role is None:
        return

    title = await _group_title(bot, chat_id)
    subs = await rbac.list_subadmins(chat_id)

    if sub == "add":
        await state.set_state(PvAdminSG.subadmin_add)
        await state.update_data(target=chat_id, pv_chat=ids[0], menu_msg=ids[1])
        await _ack(cq)
        await _edit(
            cq,
            "➕ افزودن ساب‌ادمین\n\n"
            "شناسه عددی تلگرام کاربر را بفرستید:\n"
            "<code>123456789</code>\n\n"
            f"گروه: {_esc(title)}",
            kb.subadmins_kb(chat_id, subs, back=f"pv:admin:sa:{chat_id}"),
        )
        return

    if sub == "del" and len(parts) > 5:
        target = _int_or_none(parts[5])
        if target is None:
            await _ack(cq)
            return
        try:
            await rbac.remove_subadmin(chat_id, target, requested_by=actor)
        except PermissionError:
            await _ack(cq, "❌ مجوز ندارید", alert=True)
            return
        subs = await rbac.list_subadmins(chat_id)
        await _ack(cq, "حذف شد")
        await _edit(
            cq,
            _subadmins_text(title, subs, f"ساب‌ادمین {target} حذف شد."),
            kb.subadmins_kb(chat_id, subs),
        )
        return

    await _ack(cq)
    await _edit(
        cq,
        _subadmins_text(title, subs) + "\n\nبرای افزودن، شناسه عددی را بفرستید.",
        kb.subadmins_kb(chat_id, subs),
    )


async def _welcome_route(cq: CallbackQuery, state: FSMContext, bot: Bot, parts: list[str]) -> None:
    # pv:admin:wel:<chat>   -> ask for the template (FSM)
    # pv:admin:wel:x:<chat> -> drop the template
    if len(parts) == 4:
        mode, chat_id = "", _int_or_none(parts[3])
    elif len(parts) > 4:
        mode, chat_id = parts[3], _int_or_none(parts[4])
    else:
        mode, chat_id = "", None
    if chat_id is None:
        await _ack(cq)
        return

    role = await _require_group(cq, chat_id, Role.SUBADMIN)
    if role is None:
        return

    title = await _group_title(bot, chat_id)
    policy = await settings_store.get_policy(chat_id)

    if mode == "x":
        await settings_store.set_welcome(chat_id, None)
        policy = await settings_store.get_policy(chat_id)
        await _ack(cq, "پیام حذف شد")
        await _edit(
            cq,
            _policy_text(title, policy, "پیام خوش‌آمد حذف شد."),
            kb.policy_kb(chat_id, policy),
        )
        return

    ids = await _pv_ids(cq)
    if ids is None:
        await _ack(cq)
        return
    await state.set_state(PvAdminSG.welcome_set)
    await state.update_data(target=chat_id, pv_chat=ids[0], menu_msg=ids[1])
    await _ack(cq)
    await _edit(
        cq,
        "💬 تنظیم پیام خوش‌آمد\n\n"
        "متن پیام را همین‌جا بفرستید (حداکثر ۵۰۰ کاراکتر):\n"
        "متغیرها: <code>{mention}</code> ، <code>{name}</code> ، <code>{id}</code>\n\n"
        f"گروه: {_esc(title)}",
        kb.policy_kb(chat_id, policy, back=f"pv:admin:pol:{chat_id}"),
    )


# ==================================================================== FSM =====
async def _god_staged(message: Message, state: FSMContext) -> bool:
    """Common guard for every step of the staged God Admin assignment flow."""
    user = message.from_user
    if user is None or not await rbac.is_god(user.id):
        await state.clear()
        return False
    return True


@fsm_router.message(
    F.chat.type == "private",
    StateFilter(PvGodSG.owner_chat),
    F.text,
    F.text.func(lambda t: not t.startswith("/")),
)
async def pv_owner_chat_input(message: Message, state: FSMContext, bot: Bot) -> None:
    """Step 1/3 - the target group id (e.g. -100123456789)."""
    if not await _god_staged(message, state):
        return

    raw = (message.text or "").strip()
    if not (raw.startswith("-") and raw[1:].isdigit()):
        await _notice(
            message,
            "❌ شناسه گروه باید عددی منفی باشد. مثال:\n<code>-1001234567890</code>",
        )
        return

    target_chat = int(raw)
    await state.set_state(PvGodSG.owner_user)
    await state.update_data(target_chat=target_chat)
    await message.delete()

    data = await state.get_data()
    await _edit_menu(
        bot,
        data,
        "➕ افزودن مدیر گروه — مرحله ۲ از ۳\n\n"
        f"🏘 گروه: <code>{target_chat}</code>\n\n"
        "شناسه عددی کاربر را بفرستید:\n"
        "<code>123456789</code>",
        kb.god_owner_step_kb(),
    )


@fsm_router.message(
    F.chat.type == "private",
    StateFilter(PvGodSG.owner_user),
    F.text,
    F.text.func(lambda t: not t.startswith("/")),
)
async def pv_owner_user_input(message: Message, state: FSMContext, bot: Bot) -> None:
    """Step 2/3 - the target Telegram user id."""
    if not await _god_staged(message, state):
        return

    raw = (message.text or "").strip()
    if not (raw.isdigit() and int(raw) > 0):
        await _notice(
            message,
            "❌ شناسه کاربر باید عددی مثبت باشد. مثال:\n<code>123456789</code>",
        )
        return

    data = await state.get_data()
    target_chat = int(data.get("target_chat", 0) or 0)
    if not target_chat:
        await state.clear()
        await _notice(message, "❌ مرحله اول تمام شده است؛ دوباره شروع کنید.")
        return

    target_user = int(raw)
    await state.set_state(PvGodSG.owner_level)
    await state.update_data(target_user=target_user)
    await message.delete()

    data = await state.get_data()
    await _edit_menu(
        bot,
        data,
        "➕ افزودن مدیر گروه — مرحله ۳ از ۳\n\n"
        f"🏘 گروه: <code>{target_chat}</code>\n"
        f"👤 کاربر: <code>{target_user}</code>\n\n"
        "سطح دسترسی این کاربر را برای همین گروه انتخاب کنید:",
        kb.god_owner_level_kb(target_user, target_chat),
    )


@fsm_router.message(
    F.chat.type == "private",
    StateFilter(PvGodSG.owner_level),
    F.text,
    F.text.func(lambda t: not t.startswith("/")),
)
async def pv_owner_level_hint(message: Message, state: FSMContext) -> None:
    """Step 3/3 is button-only: a typed answer must not grant anything."""
    if not await _god_staged(message, state):
        return
    await _notice(message, "❌ لطفاً یکی از دکمه‌های سطح دسترسی را بزنید.")


@fsm_router.message(
    F.chat.type == "private",
    StateFilter(PvGodSG.pay_title),
    F.text,
    F.text.func(lambda t: not t.startswith("/")),
)
async def pv_pay_title_input(message: Message, state: FSMContext, bot: Bot) -> None:
    """Payment gateway step 1/2 - the display title."""
    if not await _god_staged(message, state):
        return
    title = (message.text or "").strip()
    if not title:
        return
    await state.set_state(PvGodSG.pay_value)
    await state.update_data(pay_title=title[:64])
    await message.delete()
    data = await state.get_data()
    kind = "🪙 ولت ارز دیجیتال" if data.get("pay_type") == "crypto" else "💳 شماره کارت بانکی"
    await _edit_menu(
        bot,
        data,
        f"⚡️ مرحله ۲ از ۲ — مقدار ({kind}) را بفرستید:\n"
        "<code>شماره کارت</code> یا <code>آدرس ولت TRC20</code>",
        kb.god_payment_post_kb(),
    )


@fsm_router.message(
    F.chat.type == "private",
    StateFilter(PvGodSG.pay_value),
    F.text,
    F.text.func(lambda t: not t.startswith("/")),
)
async def pv_pay_value_input(message: Message, state: FSMContext, bot: Bot) -> None:
    """Payment gateway step 2/2 - the card number / wallet address."""
    if not await _god_staged(message, state):
        return
    value = (message.text or "").strip()
    data = await state.get_data()
    ptype = data.get("pay_type") or "card"
    valid = value.isdigit() and len(value.replace(" ", "")) >= 16 if ptype == "card" else len(value) >= 20
    if not valid:
        await _notice(
            message,
            "❌ مقدار نامعتبر است. برای کارت فقط رقم (حداقل ۱۶) و برای ولت "
            "رشته بی‌فاصله (حداقل ۲۰ کاراکتر) بفرستید.",
        )
        return
    await settings_store.payment_add(ptype, data.get("pay_title", ""), value)
    await message.delete()
    await state.clear()
    methods = await settings_store.payment_list(active_only=False)
    await _edit_menu(bot, data, _payment_methods_text(methods), kb.god_payment_kb(methods))


@fsm_router.message(
    F.chat.type == "private",
    StateFilter(PvGodSG.broadcast),
    F.text,
    F.text.func(lambda t: not t.startswith("/")),
)
async def pv_broadcast_input(message: Message, state: FSMContext, bot: Bot) -> None:
    user = message.from_user
    if user is None or not await rbac.is_god(user.id):
        await state.clear()
        return

    text = (message.text or "").strip()
    targets = sorted(await groups_store.managed_chat_ids())
    if not text:
        await _notice(message, "❌ متن پیام خالی است.")
        return
    if not targets:
        await state.clear()
        await _notice(message, "❌ گروهی برای ارسال ثبت نشده است.")
        return

    data = await state.get_data()
    await state.clear()
    await message.delete()
    await _edit_menu(
        bot, data, f"📢 در حال ارسال به {len(targets)} گروه...", kb.god_broadcast_kb()
    )
    _spawn(
        _run_broadcast(
            bot, int(data.get("pv_chat", 0)), int(data.get("menu_msg", 0)), text, targets
        ),
        name="pv-broadcast",
    )


async def _run_broadcast(
    bot: Bot, pv_chat: int, menu_id: int, text: str, targets: list[int]
) -> None:
    ok = fail = 0
    for chat_id in targets:
        try:
            # parse_mode off: a broadcast must never fail on a stray "<"
            await bot.send_message(chat_id, text, parse_mode=None)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(settings.broadcast_delay)

    summary = (
        "📢 اطلاع‌رسانی همگانی تمام شد.\n\n"
        f"✅ موفق: {ok}\n❌ ناموفق: {fail}\n🏘 گروه‌های هدف: {len(targets)}\n\n"
        f"🕐 {_now()}"
    )
    if not pv_chat or not menu_id:
        return
    try:
        await bot.edit_message_text(
            chat_id=pv_chat,
            message_id=menu_id,
            text=summary,
            reply_markup=kb.god_root_kb(has_groups=True),
        )
    except Exception as exc:
        log.debug("broadcast summary edit failed: %s", exc)


@fsm_router.message(
    F.chat.type == "private",
    StateFilter(PvAdminSG.badword_add),
    F.text,
    F.text.func(lambda t: not t.startswith("/")),
)
async def pv_badword_input(message: Message, state: FSMContext, bot: Bot) -> None:
    user = message.from_user
    data = await state.get_data()
    chat_id = int(data.get("target", 0))
    if user is None or not chat_id or await rbac.role_in(chat_id, user.id) < Role.SUBADMIN:
        await state.clear()
        return

    raw = (message.text or "").replace("،", ",").replace("|", ",")
    words = [w.strip() for w in raw.split(",") if w.strip()]
    added = await settings_store.add_badwords(chat_id, words, added_by=user.id)
    await message.delete()

    title = await _group_title(bot, chat_id)
    word_list = sorted(await settings_store.load_badwords(chat_id))
    note = f"{added} کلمه اضافه شد." if added else "کلمه تکراری بود یا وارد نشد."
    await _edit_menu(
        bot, data, _badwords_text(title, word_list, note), kb.badwords_kb(chat_id, word_list)
    )


@fsm_router.message(
    F.chat.type == "private",
    StateFilter(PvAdminSG.subadmin_add),
    F.text,
    F.text.func(lambda t: not t.startswith("/")),
)
async def pv_subadmin_input(message: Message, state: FSMContext, bot: Bot) -> None:
    user = message.from_user
    data = await state.get_data()
    chat_id = int(data.get("target", 0))
    if user is None or not chat_id or await rbac.role_in(chat_id, user.id) < Role.ADMIN:
        await state.clear()
        return

    raw = (message.text or "").strip()
    if not raw.isdigit():
        await _notice(message, "❌ فقط شناسه عددی تلگرام بفرستید: <code>123456789</code>")
        return

    target = int(raw)
    try:
        await rbac.add_subadmin(chat_id, target, granted_by=user.id)
    except (PermissionError, ValueError) as exc:
        await _notice(message, f"❌ {html.escape(str(exc))}")
        return

    await message.delete()
    title = await _group_title(bot, chat_id)
    subs = await rbac.list_subadmins(chat_id)
    await _edit_menu(
        bot,
        data,
        _subadmins_text(title, subs, f"ساب‌ادمین {target} ثبت شد."),
        kb.subadmins_kb(chat_id, subs),
    )


@fsm_router.message(
    F.chat.type == "private",
    StateFilter(PvAdminSG.welcome_set),
    F.text,
    F.text.func(lambda t: not t.startswith("/")),
)
async def pv_welcome_input(message: Message, state: FSMContext, bot: Bot) -> None:
    user = message.from_user
    data = await state.get_data()
    chat_id = int(data.get("target", 0))
    if user is None or not chat_id or await rbac.role_in(chat_id, user.id) < Role.SUBADMIN:
        await state.clear()
        return

    text = (message.text or "").strip()
    if not text:
        await _notice(message, "❌ متن نمی‌تواند خالی باشد.")
        return
    if len(text) > 500:
        await _notice(message, "❌ حداکثر ۵۰۰ کاراکتر مجاز است.")
        return

    await settings_store.set_welcome(chat_id, text)
    await state.clear()
    await message.delete()

    title = await _group_title(bot, chat_id)
    policy = await settings_store.get_policy(chat_id)
    await _edit_menu(
        bot,
        data,
        _policy_text(title, policy, "پیام خوش‌آمد ذخیره شد."),
        kb.policy_kb(chat_id, policy),
    )


@fsm_router.message(
    F.chat.type == "private",
    StateFilter(PvAdminSG.trigger_set),
    F.text,
    F.text.func(lambda t: not t.startswith("/")),
)
async def pv_trigger_input(message: Message, state: FSMContext, bot: Bot) -> None:
    """Rename the in-group panel trigger, then re-render the panel in place."""
    user = message.from_user
    data = await state.get_data()
    chat_id = int(data.get("target", 0))
    if user is None or not chat_id or await rbac.role_in(chat_id, user.id) < Role.SUBADMIN:
        await state.clear()
        return

    try:
        new_name = await trigger_store.set_trigger(chat_id, message.text or "")
    except ValueError as exc:
        await _notice(message, f"❌ {html.escape(str(exc))}")
        return
    except Exception:
        log.exception("trigger rename failed chat=%s", chat_id)
        await _notice(message, "❌ ذخیره‌سازی ناموفق بود؛ دوباره تلاش کنید.")
        return

    await state.clear()
    await message.delete()

    role = await rbac.role_in(chat_id, user.id)
    flags = await settings_store.get_flags(chat_id)
    title = await _group_title(bot, chat_id)
    await _edit_menu(
        bot,
        data,
        _group_panel_text(chat_id, role, title, flags),
        kb.group_panel_kb(chat_id, flags, role, new_name),
    )
