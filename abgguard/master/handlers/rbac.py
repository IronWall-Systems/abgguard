from __future__ import annotations

import html
import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from abgguard.core import rbac

log = logging.getLogger("abgguard.master.rbac")
router = Router(name="master.rbac")


def _mention(user) -> str:
    name = html.escape(user.full_name or "")
    return f'<a href="tg://user?id={user.id}">{name}</a>'


# ---------------------------------------------------------------- /start ---
@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Group welcome - the private chat /start opens the PV dashboard instead."""
    await message.answer(
        "🛡 ABG Guard\n\n"
        "❓ راهنمای کامل: <code>/help</code>\n"
        "🔰 سطح دسترسی شما: <code>/whoami</code>\n"
        "🛡 پنل مدیریت گروه: بنویسید «پنل» یا <code>/panel</code>\n\n"
        "دستورات مدیریتی (ساب‌ادمین و بالاتر) داخل همان راهنما توضیح داده شده است."
    )


# --------------------------------------------------------------- /whoami ---
@router.message(Command("whoami"))
async def cmd_whoami(message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    role = await rbac.get_role(message.chat.id, user.id)
    await message.answer(
        f"👤 {_mention(user)}\n"
        f"🆔 <code>{user.id}</code>\n"
        f"🔰 سطح: {role.label_fa} (Level {int(role)})\n"
        f"💬 چت: <code>{message.chat.id}</code>"
    )


# ------------------------------------------------------------ Level 3 ops --
def _parse_target(message: Message, args: str | None) -> tuple[int, int, int | None] | str:
    """
    Returns (chat_id, user_id, reply_user_id) or an error string.
    Accepts:
      /addadmin <user_id> <chat_id>      (can be issued from PM, target
                                          need not be a member of the chat)
      /addadmin                          (issued inside the group as a reply)
    """
    chat_id = message.chat.id
    reply_user = message.reply_to_message.from_user if message.reply_to_message else None
    reply_id = reply_user.id if reply_user else None

    parts = (args or "").split()
    if len(parts) >= 2 and parts[0].lstrip("-").isdigit() and parts[1].lstrip("-").isdigit():
        return int(parts[1]), int(parts[0]), reply_id
    if len(parts) >= 1 and parts[0].lstrip("-").isdigit() and reply_id is None:
        return chat_id, int(parts[0]), None
    if reply_id is not None:
        return chat_id, reply_id, reply_id
    return "استفاده: /addadmin <user_id> <chat_id>  یا ریپلای روی کاربر"


@router.message(Command("addadmin"))
async def cmd_addadmin(message: Message, command: CommandObject) -> None:
    user = message.from_user
    if user is None or not await rbac.is_god(user.id):
        await message.reply("❌ فقط گاد ادمین (Level 3) می‌تواند ادمین گروه اضافه کند.")
        return

    parsed = _parse_target(message, command.args)
    if isinstance(parsed, str):
        await message.reply(parsed)
        return
    chat_id, target_id, _ = parsed

    try:
        await rbac.add_admin(chat_id, target_id, granted_by=user.id)
    except (PermissionError, ValueError) as exc:
        await message.reply(f"❌ {exc}")
        return

    await message.reply(
        f"✅ کاربر <code>{target_id}</code> به عنوان ادمین (Level 2) گروه "
        f"<code>{chat_id}</code> ثبت شد.\n"
        "او اکنون می‌تواند ساب‌ادمین اضافه کند."
    )
    log.info("GOD %s granted ADMIN %s on %s", user.id, target_id, chat_id)


@router.message(Command("deladmin"))
async def cmd_deladmin(message: Message, command: CommandObject) -> None:
    user = message.from_user
    if user is None or not await rbac.is_god(user.id):
        await message.reply("❌ فقط گاد ادمین می‌تواند ادمین حذف کند.")
        return

    parsed = _parse_target(message, command.args)
    if isinstance(parsed, str):
        await message.reply(parsed.replace("/addadmin", "/deladmin"))
        return
    chat_id, target_id, _ = parsed
    await rbac.remove_admin(chat_id, target_id, requested_by=user.id)
    await message.reply(f"🗑 ادمین <code>{target_id}</code> از گروه <code>{chat_id}</code> حذف شد.")


@router.message(Command("god"))
async def cmd_god(message: Message, command: CommandObject) -> None:
    user = message.from_user
    if user is None or not await rbac.is_god(user.id):
        await message.reply("❌ فقط گاد ادمین می‌تواند گاد ادمین بسازد.")
        return
    parts = (command.args or "").split()
    if not parts or not parts[0].isdigit():
        await message.reply("استفاده: /god <user_id>")
        return
    target = int(parts[0])
    await rbac.add_god(target, granted_by=user.id, note="granted via /god")
    await message.reply(f"👑 کاربر <code>{target}</code> اکنون گاد ادمین (Level 3) است.")


@router.message(Command("ungod"))
async def cmd_ungod(message: Message, command: CommandObject) -> None:
    user = message.from_user
    if user is None or not await rbac.is_god(user.id):
        await message.reply("❌ مجوز ندارید.")
        return
    parts = (command.args or "").split()
    if not parts or not parts[0].isdigit():
        await message.reply("استفاده: /ungod <user_id>")
        return
    target = int(parts[0])
    if target == user.id:
        await message.reply("❌ نمی‌توانید خودتان را حذف کنید.")
        return
    await rbac.remove_god(target)
    await message.reply(f"🗑 سطح گاد ادمین کاربر <code>{target}</code> برداشته شد.")


# ------------------------------------------------------------ Level 2 ops --
@router.message(Command("addsub"))
async def cmd_addsub(message: Message, command: CommandObject) -> None:
    user = message.from_user
    if user is None:
        return
    chat_id = message.chat.id
    if not await rbac.can_manage_subadmins(chat_id, user.id):
        await message.reply("❌ فقط ادمین گروه (Level 2) می‌تواند ساب‌ادمین اضافه کند.")
        return

    reply_user = message.reply_to_message.from_user if message.reply_to_message else None
    parts = (command.args or "").split()
    if reply_user is not None:
        target = reply_user.id
    elif parts and parts[0].isdigit():
        target = int(parts[0])
    else:
        await message.reply("استفاده: ریپلای روی کاربر + /addsub  یا  /addsub <user_id>")
        return

    try:
        await rbac.add_subadmin(chat_id, target, granted_by=user.id)
    except (PermissionError, ValueError) as exc:
        await message.reply(f"❌ {exc}")
        return
    await message.reply(
        f"✅ کاربر <code>{target}</code> به عنوان ساب‌ادمین (Level 1) ثبت شد."
    )


@router.message(Command("rmsub"))
async def cmd_rmsub(message: Message, command: CommandObject) -> None:
    user = message.from_user
    if user is None:
        return
    chat_id = message.chat.id
    if not await rbac.can_manage_subadmins(chat_id, user.id):
        await message.reply("❌ مجوز ندارید.")
        return

    reply_user = message.reply_to_message.from_user if message.reply_to_message else None
    parts = (command.args or "").split()
    if reply_user is not None:
        target = reply_user.id
    elif parts and parts[0].isdigit():
        target = int(parts[0])
    else:
        await message.reply("استفاده: ریپلای + /rmsub  یا  /rmsub <user_id>")
        return

    await rbac.remove_subadmin(chat_id, target, requested_by=user.id)
    await message.reply(f"🗑 ساب‌ادمین <code>{target}</code> حذف شد.")


@router.message(Command("panel"))
async def cmd_panel(message: Message, state: FSMContext) -> None:
    """Fallback trigger - the primary trigger is the word "پنل" (panel router)."""
    from abgguard.master.handlers.panel import open_panel

    await open_panel(message, state)
