from __future__ import annotations

import html
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from abgguard.core import groups as groups_store
from abgguard.core import rbac, settings_store
from abgguard.core import trigger as trigger_store
from abgguard.core.rbac import Role
from abgguard.core.settings_store import INV_RKEY, GroupFlags
from abgguard.master import ui
from abgguard.master.ephemeral import fa_num, notice
from abgguard.master.keyboards import (
    TOGGLES,
    badwords_menu_kb,
    panel_kb,
    state_label,
    subadmin_menu_kb,
    trigger_menu_kb,
)
from abgguard.master.states import PanelSG
from abgguard.services import subscription
from abgguard.services.textguards import is_bot_message

log = logging.getLogger("abgguard.master.panel")

fsm_router = Router(name="master.panel-fsm")
router = Router(name="master.panel")

PANEL_TITLE = "🛡 پنل مدیریت گروه"

# step 2: strict RBAC - unregistered users are kept out of the panel
RBAC_REJECT = "⛔️ فقط ادمین‌ها و ساب‌ادمین‌های ثبت‌شده به پنل دسترسی دارند."

HELP_TEXT = (
    "❓ راهنما\n\n"
    "🔵 وضعیت هر دکمه:\n"
    "🟢 فعال = قفل روشن است\n"
    "🔴 غیرفعال = قفل باز است\n\n"
    "🔞 پورن: اسکن محلی تصاویر توسط هوش مصنوعی\n"
    "🖼 🎥 🎞: مسدود کردن نوع رسانه\n"
    "😀 ایموجی: مسدود کردن ایموجی در متن\n"
    "🔗 لینک: حذف پیام حاوی لینک\n"
    "🔄 فوروارد: حذف پیام فوروارد شده\n\n"
    "🤬 کلمات رکیک: فهرست سیاه گروه\n"
    "👥 ساب‌ادمین‌ها: فقط برای ادمین گروه (Level 2)"
)


# ------------------------------------------------------------------ utils --
class PanelTrigger(Filter):
    """Matches the group's panel trigger - the per-group custom alias.

    The default alias is "پنل" (project rule 6); Level 1/2 admins may rename it
    from the panel or the PV dashboard. Every miss degrades to the default, so
    a broken cache can never make the panel unreachable.
    """

    async def __call__(self, message: Message) -> bool:
        chat, text = message.chat, message.text
        if chat is None or text is None:
            return False
        if chat.type not in {"group", "supergroup"}:
            return False
        return await trigger_store.matches(chat.id, text)


async def _render(target: Message, chat_id: int, role: Role) -> bool:
    flags = await settings_store.get_flags(chat_id)
    trigger = await trigger_store.get_trigger(chat_id)
    try:
        await target.edit_text(
            _panel_text(chat_id, role, flags),
            reply_markup=panel_kb(flags, role, trigger),
        )
        return True
    except Exception as exc:  # message not modified, identical text, ...
        log.debug("panel render skipped: %s", exc)
        return False


def _panel_text(chat_id: int, role: Role, flags: GroupFlags) -> str:
    lines = [
        PANEL_TITLE,
        f"🔰 سطح دسترسی شما: {role.label_fa} (Level {int(role)})",
        f"🆔 شناسه گروه: <code>{chat_id}</code>",
        "",
        "وضعیت قفل‌ها:",
    ]
    for field, emoji, label in TOGGLES:
        lines.append(f"{emoji} {label}: {state_label(flags.is_on(field))}")
    return "\n".join(lines)


async def open_panel(message: Message, state: FSMContext | None = None) -> None:
    """Trigger: an Admin/Sub-Admin writes this group's trigger alias."""
    if state is not None:
        await state.clear()

    user = message.from_user
    if is_bot_message(message) or user is None:
        return  # the bots never act on each other's messages (watchdog rule)
    chat_id = message.chat.id
    role = await rbac.get_role(chat_id, user.id)
    if role < Role.SUBADMIN:
        return  # ordinary members get silence, not an error

    await message.delete()
    await groups_store.upsert_group(chat_id, title=message.chat.title)

    # step 3: EXPIRED/UNPAID groups get the exact pause notice, never the panel
    # (rate-limited: at most one notice every 10 minutes per chat - no spam)
    await subscription.record_join(chat_id, message.chat.title)
    if not await subscription.is_active(chat_id):
        await subscription.send_expiry_notice(message.bot, chat_id)
        return

    flags = await settings_store.get_flags(chat_id)
    trigger = await trigger_store.get_trigger(chat_id)
    await message.answer(
        _panel_text(chat_id, role, flags), reply_markup=panel_kb(flags, role, trigger)
    )
    log.info(
        "PANEL open chat=%s trigger=%s by=%s role=%s",
        chat_id,
        message.message_id,
        user.id,
        role,
    )


# --------------------------------------------------------------- trigger ---
@router.message(PanelTrigger())
async def panel_trigger(message: Message, state: FSMContext) -> None:
    await open_panel(message, state)


# ------------------------------------------------------------------ FSM ----
@fsm_router.message(StateFilter(PanelSG.bad_word), F.text)
async def badword_input(message: Message, state: FSMContext, bot: Bot) -> None:
    chat_id = message.chat.id
    user = message.from_user
    if user is None:
        return

    if await trigger_store.matches(chat_id, message.text):
        await open_panel(message, state)
        return
    if not await rbac.can_open_panel(chat_id, user.id):
        await state.clear()
        return

    raw = (message.text or "").replace("،", ",").replace("|", ",")
    words = [w.strip() for w in raw.split(",") if w.strip()]
    added = await settings_store.add_badwords(chat_id, words, added_by=user.id)

    await message.delete()
    data = await state.get_data()
    menu_id = data.get("menu_msg")

    status = f"✅ {added} کلمه اضافه شد." if added else "⚠️ این کلمه قبلاً ثبت شده بود."
    if menu_id:
        words_now = sorted(await settings_store.load_badwords(chat_id))
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=menu_id,
                text=(
                    "🤬 مدیریت کلمات رکیک\n"
                    f"تعداد: {len(words_now)}\n\n"
                    "کلمات جدید را (جدا شده با کاما) بفرستید:\n"
                    f"{status}"
                ),
                reply_markup=badwords_menu_kb(words_now),
            )
        except Exception as exc:
            log.debug("badword menu refresh failed: %s", exc)
    else:
        await message.answer(f"🤬 {status}")


@fsm_router.message(StateFilter(PanelSG.sub_admin_add), F.text | F.reply_to_message)
async def subadmin_input(message: Message, state: FSMContext, bot: Bot) -> None:
    chat_id = message.chat.id
    actor = message.from_user
    if actor is None:
        return

    if await trigger_store.matches(chat_id, message.text):
        await open_panel(message, state)
        return
    if not await rbac.can_manage_subadmins(chat_id, actor.id):
        await state.clear()
        return

    reply_user = message.reply_to_message.from_user if message.reply_to_message else None
    if reply_user is not None and not reply_user.is_bot:
        target = reply_user.id
    elif message.text and message.text.strip().isdigit():
        target = int(message.text.strip())
    else:
        await message.reply(
            "❌ ریپلای روی کاربر بزنید یا شناسه عددی تلگرام را بفرستید:\n"
            "<code>/addsub 123456789</code>"
        )
        return

    try:
        await rbac.add_subadmin(chat_id, target, granted_by=actor.id)
    except (PermissionError, ValueError) as exc:
        await message.reply(f"❌ {html.escape(str(exc))}")
        return

    await message.delete()
    data = await state.get_data()
    menu_id = data.get("menu_msg")
    subs = await rbac.list_subadmins(chat_id)
    text = (
        "👥 مدیریت ساب‌ادمین‌ها\n"
        f"تعداد: {len(subs)}\n\n"
        "✅ ثبت شد. برای افزودن، ریپلای روی کاربر یا شناسه عددی بفرستید."
    )
    if menu_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=menu_id,
                text=text,
                reply_markup=subadmin_menu_kb(subs),
            )
        except Exception as exc:
            log.debug("subadmin menu refresh failed: %s", exc)
    else:
        await message.answer(text, reply_markup=subadmin_menu_kb(subs))


@fsm_router.message(StateFilter(PanelSG.trigger_name), F.text)
async def trigger_name_input(message: Message, state: FSMContext, bot: Bot) -> None:
    """Save the new per-group alias, then refresh the panel in place (rule 6)."""
    chat_id = message.chat.id
    user = message.from_user
    if user is None:
        return
    if not await rbac.can_open_panel(chat_id, user.id):
        await state.clear()
        return

    data = await state.get_data()
    try:
        new_name = await trigger_store.set_trigger(chat_id, message.text or "")
    except ValueError as exc:
        await notice(message, f"❌ {html.escape(str(exc))}")
        return
    except Exception:
        log.exception("trigger rename failed chat=%s", chat_id)
        await notice(message, "❌ ذخیره‌سازی ناموفق بود؛ دوباره تلاش کنید.")
        return

    await state.clear()
    await message.delete()

    role = await rbac.get_role(chat_id, user.id)
    flags = await settings_store.get_flags(chat_id)
    menu_id = data.get("menu_msg")
    if menu_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(menu_id),
                text=_panel_text(chat_id, role, flags),
                reply_markup=panel_kb(flags, role, new_name),
            )
        except Exception as exc:
            log.debug("panel refresh after rename failed: %s", exc)
    else:
        await message.answer(
            _panel_text(chat_id, role, flags),
            reply_markup=panel_kb(flags, role, new_name),
        )


# -------------------------------------------------------------- callbacks ---
async def _ack(cq: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
    try:
        await cq.answer(text, show_alert=alert)
    except Exception:  # already answered or query too old
        pass


@router.callback_query(F.data.startswith("abg:"))
async def on_panel_callback(cq: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    msg = cq.message
    if not isinstance(msg, Message) or msg.chat is None or cq.from_user is None:
        await _ack(cq)
        return

    chat_id = msg.chat.id
    actor = cq.from_user
    role = await rbac.get_role(chat_id, actor.id)
    if role < Role.SUBADMIN:
        await _ack(cq, RBAC_REJECT, alert=True)
        return
    if not await subscription.is_active(chat_id):
        if msg.chat.type == "private":
            await _ack(cq, subscription.EXPIRY_NOTICE_TEXT, alert=True)
        else:
            await subscription.send_expiry_notice(cq.bot, chat_id)
            await _ack(cq)
        return
    parts = (cq.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""

    # ---- destructive full-history wipe confirmation ----------------------
    if action == "wipe" and len(parts) > 2:
        if msg.chat.type not in {"group", "supergroup"}:
            await _ack(cq, "❌ این عملیات فقط در گروه قابل اجراست.", alert=True)
            return
        if parts[2] == "no":
            await _ack(cq, "لغو شد")
            try:
                await msg.delete()
            except Exception as exc:
                log.debug("wipe cancel delete failed: %s", exc)
            return
        if parts[2] == "yes":
            await _ack(cq, "پاک‌سازی آغاز شد")
            try:
                await msg.edit_text("⏳ در حال پاک‌سازی تاریخچه...", reply_markup=None)
            except Exception as exc:
                log.debug("wipe progress edit failed: %s", exc)
            from abgguard.master.handlers import modcmd

            await modcmd._do_wipe_all(msg, bot)
            return
        await _ack(cq)
        return

    # ---- subscription card (step 3) --------------------------------------
    if action == "sub":
        title = msg.chat.title or str(chat_id)
        row = await subscription.record_join(chat_id, title)
        await _ack(cq)
        try:
            await cq.bot.send_message(
                chat_id,
                ui.pay_view_text(chat_id, title, row.status, row.expires_at),
                reply_markup=ui.renew_kb(chat_id),
            )
        except Exception as exc:
            log.debug("subscription card failed: %s", exc)
        return

    # ---- boolean toggles -------------------------------------------------
    if action == "lock" and len(parts) > 2:
        if role < Role.SUBADMIN:
            await _ack(cq, "❌ دسترسی ندارید", alert=True)
            return
        field = INV_RKEY.get(parts[2])
        if field is None:
            await _ack(cq)
            return
        flags = await settings_store.toggle(chat_id, field)
        trigger = await trigger_store.get_trigger(chat_id)
        await _ack(cq, "تغییر ذخیره شد ✅")
        await msg.edit_text(
            _panel_text(chat_id, role, flags),
            reply_markup=panel_kb(flags, role, trigger),
        )
        return

    # ---- bad words -------------------------------------------------------
    if action == "bad":
        if role < Role.SUBADMIN:
            await _ack(cq, "❌ دسترسی ندارید", alert=True)
            return
        words = sorted(await settings_store.load_badwords(chat_id))
        await state.set_state(PanelSG.bad_word)
        await state.update_data(menu_msg=msg.message_id)
        await msg.edit_text(
            "🤬 مدیریت کلمات رکیک\n"
            f"تعداد: {len(words)}\n\n"
            "کلمات جدید را (جدا شده با کاما) بفرستید:\n"
            "برای خروج دکمه ✅ پایان را بزنید.",
            reply_markup=badwords_menu_kb(words),
        )
        await _ack(cq)
        return

    if action == "bw" and len(parts) > 2:
        if role < Role.SUBADMIN:
            await _ack(cq, "❌ دسترسی ندارید", alert=True)
            return
        if parts[2] == "del" and len(parts) > 3:
            await settings_store.remove_badword(chat_id, ":".join(parts[3:]))
        elif parts[2] == "clear":
            for w in list(await settings_store.load_badwords(chat_id)):
                await settings_store.remove_badword(chat_id, w)
        words = sorted(await settings_store.load_badwords(chat_id))
        await _ack(cq, "حذف شد")
        await msg.edit_text(
            f"🤬 مدیریت کلمات رکیک\nتعداد: {len(words)}\n\n"
            "کلمات جدید را (جدا شده با کاما) بفرستید:",
            reply_markup=badwords_menu_kb(words),
        )
        return

    # ---- sub-admins (Level 2 + Level 3 only) -----------------------------
    if action == "sa":
        if role < Role.ADMIN:
            await _ack(cq, "❌ فقط ادمین گروه (Level 2)", alert=True)
            return
        if len(parts) == 2:
            subs = await rbac.list_subadmins(chat_id)
            await _ack(cq)
            await msg.edit_text(
                "👥 مدیریت ساب‌ادمین‌ها\n"
                f"تعداد: {len(subs)}\n\n"
                "برای افزودن: ریپلای روی کاربر یا شناسه عددی.",
                reply_markup=subadmin_menu_kb(subs),
            )
            return
        if parts[2] == "del" and len(parts) > 3:
            target = int(parts[3])
            try:
                await rbac.remove_subadmin(chat_id, target, requested_by=actor.id)
            except PermissionError:
                await _ack(cq, "❌ مجوز ندارید", alert=True)
                return
            subs = await rbac.list_subadmins(chat_id)
            await _ack(cq, "حذف شد")
            await msg.edit_text(
                f"👥 مدیریت ساب‌ادمین‌ها\nتعداد: {len(subs)}",
                reply_markup=subadmin_menu_kb(subs),
            )
            return
        if parts[2] == "add":
            await state.set_state(PanelSG.sub_admin_add)
            await state.update_data(menu_msg=msg.message_id)
            await _ack(cq)
            await msg.edit_text(
                "➕ افزودن ساب‌ادمین\n\n"
                "روی پیام کاربر ریپلای کنید یا شناسه عددی تلگرام را بفرستید:\n"
                "<code>123456789</code>\n\n"
                "برای خروج دکمه 🔙 را بزنید.",
                reply_markup=subadmin_menu_kb(await rbac.list_subadmins(chat_id)),
            )
            return

    # ---- custom trigger name (Level 1 + Level 2 + Level 3) ---------------
    if action == "trg":
        if role < Role.SUBADMIN:
            await _ack(cq, "❌ دسترسی ندارید", alert=True)
            return
        current = await trigger_store.get_trigger(chat_id)
        await state.set_state(PanelSG.trigger_name)
        await state.update_data(menu_msg=msg.message_id)
        await _ack(cq)
        await msg.edit_text(
            "🏷 نام فراخوانی ربات\n\n"
            f"نام فعلی: <code>{html.escape(current)}</code>\n\n"
            "نام جدید را بفرستید "
            f"({fa_num(trigger_store.TRIGGER_MIN)} تا "
            f"{fa_num(trigger_store.TRIGGER_MAX)} کاراکتر):\n"
            "برای انصراف دکمه 🔙 را بزنید.",
            reply_markup=trigger_menu_kb(),
        )
        return

    # ---- close: an admin dismisses the panel entirely (rule 6) -----------
    if action == "close":
        if role < Role.SUBADMIN:
            await _ack(cq, "❌ دسترسی ندارید", alert=True)
            return
        await state.clear()
        await _ack(cq, "پنل بسته شد ✅")
        try:
            await msg.delete()
        except Exception as exc:
            log.debug("panel close delete failed: %s", exc)
            # the chat must still look clean: fall back to an inert message
            try:
                await msg.edit_text("✅ پنل بسته شد.", reply_markup=None)
            except Exception as exc2:
                log.debug("panel close fallback failed: %s", exc2)
        return

    # ---- navigation ------------------------------------------------------
    if action in {"refresh", "back"}:
        await state.clear()
        if role < Role.SUBADMIN:
            await _ack(cq, "❌ دسترسی ندارید", alert=True)
            return
        await _ack(cq, "بروزرسانی شد")
        await _render(msg, chat_id, role)
        return

    if action == "help":
        await _ack(cq)
        await msg.edit_text(
            HELP_TEXT,
            reply_markup=panel_kb(
                await settings_store.get_flags(chat_id),
                role,
                await trigger_store.get_trigger(chat_id),
            ),
        )
        return

    await _ack(cq)
