from __future__ import annotations

import logging

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import SimpleFilesPathWrapper, TelegramAPIServer

from abgguard.config import settings

log = logging.getLogger("abgguard.botapi")


def make_session() -> AiohttpSession | None:
    """Route Bot API calls through the local server when configured.

    A local Bot API server (``--local`` mode) removes the cloud 48-hour
    deletion limit and is required for deep purges.  Returning ``None`` lets
    aiogram fall back to its default cloud session.

    In local mode Telegram returns absolute container paths such as
    ``/var/lib/telegram-bot-api/<token>/photos/file_1.jpg``.  Building an HTTP
    ``/file/bot...`` URL from that path produces a 404, so aiogram must read
    the file from the host bind mount instead.  ``SimpleFilesPathWrapper``
    translates the server path to the host path.
    """
    base = settings.bot_api_base.strip()
    if not base:
        return None

    local_dir = settings.bot_api_data_dir
    if local_dir.is_dir():
        log.info("local Bot API media files: %s -> %s", settings.bot_api_server_dir, local_dir)
        server = TelegramAPIServer.from_base(
            base,
            is_local=True,
            wrap_local_file=SimpleFilesPathWrapper(
                server_path=settings.bot_api_server_dir,
                local_path=local_dir,
            ),
        )
    else:
        log.warning(
            "local Bot API media dir missing (%s); media downloads will use HTTP",
            local_dir,
        )
        server = TelegramAPIServer.from_base(base)

    return AiohttpSession(api=server)
