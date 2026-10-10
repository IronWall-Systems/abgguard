from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from abgguard.worker.handlers import router as media_router


def create_worker(token: str) -> tuple[Bot, Dispatcher]:
    """
    WORKER (MEDIA) BOT - handles ONLY media:
      * photos, videos, gifs, video notes, documents, stickers
      * runs the local AI nudity scan and deletes offending media
      * flags Redis so the Master bot performs the actual ban
    No FSM, no panel, no text commands - the hot path stays allocation light.
    """
    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(media_router)
    return bot, dp
