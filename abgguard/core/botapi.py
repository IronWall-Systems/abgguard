from __future__ import annotations

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from abgguard.config import settings


def make_session() -> AiohttpSession | None:
    """Route Bot API calls through the local server when configured.

    A local Bot API server (``--local`` mode) removes the cloud 48-hour
    deletion limit and is required for deep purges.  Returning ``None`` lets
    aiogram fall back to its default cloud session.
    """
    base = settings.bot_api_base.strip()
    if not base:
        return None
    return AiohttpSession(api=TelegramAPIServer.from_base(base))
