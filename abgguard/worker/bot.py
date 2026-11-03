from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from abgguard.core.botapi import make_session
from abgguard.worker.fallback import router as fallback_router
from abgguard.worker.handlers import router as media_router


def create_worker(token: str) -> tuple[Bot, Dispatcher]:
    """
    WORKER (MEDIA) BOT - handles ONLY media:
      * photos, videos, gifs, video notes, documents, stickers
      * runs the local AI nudity scan and deletes offending media
      * flags Redis so the Master bot performs the actual ban
    No FSM, no panel, no text commands - the hot path stays allocation light.
    ...except in FALLBACK MODE: when `abg:heartbeat:master` expires (master
    server down in the future two-server layout), `worker/fallback.py` takes
    over the full text-guard + mod-command side until the master returns.
    """
    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=make_session(),
    )
    dp = Dispatcher()
    # text fallback router first - its heartbeat gate makes it a no-op while
    # the master is alive; the media router below stays the normal hot path.
    dp.include_router(fallback_router)
    dp.include_router(media_router)
    return bot, dp
