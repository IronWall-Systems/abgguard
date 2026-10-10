from __future__ import annotations

import asyncio
import html
import logging
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from abgguard.config import settings
from abgguard.core import groups as groups_store
from abgguard.core import rbac, settings_store
from abgguard.core import redis as rk
from abgguard.core import trigger as trigger_store
from abgguard.core.rbac import Role
from abgguard.master import pv_keyboards as kb
from abgguard.master.ephemeral import fa_num
from abgguard.master.handlers.help import render_menu
from abgguard.master.states.pv_states import PvAdminSG, PvGodSG
from abgguard.models.entities import BanLog
from abgguard.services import metrics
from abgguard.services.antiporn import porn_client
from abgguard.services.harvester import harvester

log = logging.getLogger("abgguard.master.pv")

# The PV routers are mounted BEFORE the group routers: a private-chat message
# never reaches the in-group panel, the text guards or the watchdog bot.
fsm_router = Router(name="master.pv-fsm")
router = Router(name="master.pv")

TRIGGER = "پنل"

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
    bindings = await rbac.list_owner_bindings()
    titles = await groups_store.known_titles([c for c, _ in bindings])
    return [
        (chat_id, user_id, titles.get(chat_id, str(chat_id)))
        for chat_id, user_id in bindings
    ]


async def _owners_text(note: str | None = None) -> str:
    rows = await _owners_rows()
    lines = ["👥 مدیران کل گروه‌ها (Level 2)", f"تعداد: {len(rows)}", ""]
    for chat_id, user_id, title in rows[:20]:
        lines.append(
            f"• {_esc(title)} → کاربر <code>{user_id}</code> | گروه <code>{chat_id}</code>"
        )
    if not rows:
        lines.append("هنوز مدیر گروهی ثبت نشده است.")
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
async def _open_pv(message: Message, state: FSMContext, bot: Bot) -> None:
    print(f"PV Message received from {message.from_user.id}", flush=True)
    user = message.from_user
    if user is None or user.is_bot:
        return
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


@router.callback_query(F.data == "pv:root")
async def pv_root(cq: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    user = cq.from_user
    if user is None:
        await _ack(cq)
        return
    text, markup = await _root_parts(bot, user, state)
    await _ack(cq)
    await _edit(cq, text, markup)


@router.callback_query(F.data == "pv:help")
async def pv_help(cq: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Dashboard "help" -> the full role-aware, paginated help menu."""
    await render_menu(cq, state, bot)


# ============================================================== GOD (Level 3) ==
@router.callback_query(F.data.startswith("pv:god:"))
async def pv_god_callback(cq: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    user = cq.from_user
    ids = await _pv_ids(cq)
    if user is None or ids is None:
        await _ack(cq)
        return
    if not await rbac.is_god(user.id):
        await _ack(cq, "❌ دسترسی ندارید", alert=True)
        return

    pv_chat, menu_id = ids
    parts = (cq.data or "").split(":")
    action = parts[2] if len(parts) > 2 else ""
    await state.clear()

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
        await _ack(cq, "ثبت شد ✅")
        await _edit(
            cq, await _owners_text(note), kb.god_owners_kb(await _owners_rows())
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
        await _ack(cq)
        await _edit(
            cq,
            "👤 جزئیات مدیر گروه\n\n"
            f"🆔 کاربر: <code>{target}</code>\n"
            f"🏘 گروه: {_esc(title)}\n"
            f"🆔 شناسه گروه: <code>{chat_id}</code>",
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
        await _ack(cq, "❌ دسترسی ندارید", alert=True)
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
