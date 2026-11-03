"""DISTRIBUTED FALLBACK for @ABGGUARD1BOT (the worker bot).

Normal mode (master heartbeat alive): the worker stays *silent* on text -
only the media/pornscan watchdog handlers run, so a message is never answered
by both bots (rule §1).

Fallback mode (`abg:heartbeat:master` expired - the master server is down in
the future two-server layout): the worker takes over the ENTIRE text side -
mod reply commands (حذف / سکوت / لغو سکوت / بن / ویژه) and the text-guard
ladder - until the master heartbeat returns. The takeover is dynamic: every
incoming text re-checks the heartbeat first, so when the master rotates back
online the worker simply falls silent again on the next message.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import Message

from abgguard.core import redis as rk
from abgguard.master.handlers import guards, modcmd
from abgguard.services.textguards import is_bot_message

log = logging.getLogger("abgguard.worker.fallback")

router = Router(name="worker.fallback")
router.message.filter(F.chat.type.in_({"group", "supergroup"}), F.text)


@router.message()
async def fallback_text(message: Message, bot) -> None:
    """Only when the master heartbeat is dead does this handler ever act."""
    if await rk.is_master_alive():
        return

    chat = message.chat
    user = message.from_user
    if chat is None or user is None or is_bot_message(message):
        return

    text = message.text or ""
    log.warning(
        "FALLBACK MODE active - worker answering text chat=%s user=%s",
        chat.id,
        user.id,
    )

    # 1. reply-driven staff tools first (VIP is also reply-driven)
    if message.reply_to_message is not None:
        if modcmd.vip_action(text):
            await modcmd.on_vip_command(message, bot)
            return
        if modcmd.is_remove_trigger(text):
            await modcmd.on_remove_command(message, bot)
            return
        if modcmd.is_pin_trigger(text):
            await modcmd.on_pin_command(message, bot)
            return

    # 1b. unpin also works without a reply (unpins the latest pinned message)
    if modcmd.is_unpin_trigger(text):
        await modcmd.on_unpin_command(message, bot)
        return

    # 2. slash / bare moderation commands (staff-only enforced inside)
    if modcmd.parse_mod_command(text):
        await modcmd.on_mod_command(message, bot)
        return

    if text.startswith("/"):
        return

    # 3. the ordinary text-guard ladder - identical logic to the master
    await guards.guard_text(message, bot)
