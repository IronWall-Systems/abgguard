from __future__ import annotations

import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import ErrorEvent, Update

from abgguard.config import FALLBACK_GOD_ADMIN_ID, settings
from abgguard.core.botapi import make_session
from abgguard.core.redis import get_redis
from abgguard.master.handlers import (
    guards,
    media_fallback,
    members,
    modcmd,
    panel,
    pv_panel,
    rbac,
)
from abgguard.master.handlers.help import router as help_router

log = logging.getLogger("abgguard.master.dispatch")

ERROR_NOTICE = "⚠️ خطای داخلی ربات رخ داد و درخواست شما اجرا نشد؛ لطفاً دوباره تلاش کنید."


def _update_object(update: Update):
    kind = update.event_type
    return getattr(update, kind, None) if kind else None


def _describe(update: Update) -> str:
    """One line that is enough to explain *which* update we are talking about."""
    obj = _update_object(update)
    parts = [f"update_id={update.update_id}", f"type={update.event_type}"]

    chat = getattr(obj, "chat", None)
    if update.event_type == "callback_query" and obj is not None:
        chat = getattr(getattr(obj, "message", None), "chat", None)
    if chat is not None:
        parts.append(f"chat={chat.id}({chat.type})")

    user = getattr(obj, "from_user", None)
    if user is not None:
        parts.append(f"user={user.id}")

    text = getattr(obj, "text", None) or getattr(obj, "data", None)
    if isinstance(text, str):
        parts.append(f"text={text[:48]!r}")
    return " ".join(parts)


def _is_private_pv(update: Update) -> bool:
    obj = _update_object(update)
    chat = getattr(obj, "chat", None)
    if update.event_type == "callback_query" and obj is not None:
        chat = getattr(getattr(obj, "message", None), "chat", None)
    return chat is not None and chat.type == "private"


def _from_user_id(update: Update) -> int | None:
    obj = _update_object(update)
    user = getattr(obj, "from_user", None)
    return user.id if user is not None else None


def install_diagnostics(dp: Dispatcher) -> None:
    """
    Make every silent drop explain itself.

    * an update no router/filter matched  -> "UPDATE DROPPED ... reason=..."
    * an update that raised              -> "UPDATE FAILED" + "UPDATE ERROR" (+traceback)
    * any update from a Level 3 account in PV -> an explicit trace line with the outcome
    """
    watched = set(settings.god_admin_ids) | {FALLBACK_GOD_ADMIN_ID}

    @dp.update.outer_middleware
    async def _trace(handler, event, data):
        try:
            response = await handler(event, data)
        except Exception:
            log.warning("UPDATE FAILED  %s", _describe(event), exc_info=True)
            raise

        if response is UNHANDLED:
            log.warning(
                "UPDATE DROPPED  %s  reason=no handler matched "
                "(router order / filter rejected it)",
                _describe(event),
            )

        if _from_user_id(event) in watched and _is_private_pv(event):
            outcome = "DROPPED (no handler matched)" if response is UNHANDLED else "handled"
            log.info("PV trace  %s -> %s", _describe(event), outcome)
        return response

    @dp.errors()
    async def _on_error(event: ErrorEvent, bot: Bot) -> None:
        update = event.update
        log.error(
            "UPDATE ERROR   %s  %s: %s",
            _describe(update),
            type(event.exception).__name__,
            event.exception,
            exc_info=event.exception,
        )
        # never leave the user with silence - but never let the notice itself fail
        message = update.message
        if message is not None and message.chat.type == "private":
            with suppress(Exception):
                await bot.send_message(message.chat.id, ERROR_NOTICE)


def create_master(token: str) -> tuple[Bot, Dispatcher]:
    """
    MASTER BOT - handles ONLY:
      * private chat (PV) dashboard: /start, /panel, pv:* callbacks, FSM prompts
      * text messages + group management commands
      * the in-group inline admin panel
      * join / leave events
    It never touches media, so a media flood can never starve it.
    """
    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=make_session(),
    )
    dp = Dispatcher(storage=RedisStorage(redis=get_redis()))

    # router order is part of the contract:
    #   help -> PV dashboard -> commands -> moderation -> active FSM
    #        -> panel trigger -> guards -> membership
    dp.include_router(help_router)
    dp.include_router(pv_panel.fsm_router)
    dp.include_router(pv_panel.router)
    dp.include_router(rbac.router)
    dp.include_router(modcmd.router)
    dp.include_router(panel.fsm_router)
    dp.include_router(panel.router)
    dp.include_router(media_fallback.router)
    dp.include_router(guards.router)
    dp.include_router(members.router)

    install_diagnostics(dp)
    return bot, dp
