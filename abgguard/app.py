from __future__ import annotations

import asyncio
import logging
import signal
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from abgguard.config import settings
from abgguard.core import rbac
from abgguard.core.db import close_db, init_db
from abgguard.core.redis import close_redis
from abgguard.master.bot import create_master
from abgguard.services.antiporn import porn_client
from abgguard.services.bus import BanEventBus
from abgguard.services.harvester import harvester
from abgguard.worker.bot import create_worker

log = logging.getLogger("abgguard.app")

# Update types the master bot must always subscribe to, whatever the routers
# currently register. Forcing them means a refactor that accidentally drops a
# PV handler can never make the bot fall silent in private chats - the update
# still reaches the dispatcher and is reported by the diagnostics middleware.
PV_UPDATE_TYPES: tuple[str, ...] = ("callback_query", "message")


def allowed_updates(dp: Dispatcher, *, require_pv: bool = False) -> list[str]:
    """Explicit, non-empty allowed_updates list for polling / webhook."""
    used = set(dp.resolve_used_update_types())
    if require_pv:
        used.update(PV_UPDATE_TYPES)
    return sorted(used)


def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)-28s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def _install_stop_signal() -> asyncio.Event:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    return stop


# ---------------------------------------------------------------------------
# Mode A - long polling (default, zero infrastructure)
# ---------------------------------------------------------------------------
async def _clear_stale_webhook(bot: Bot, name: str) -> None:
    """
    Telegram refuses to deliver updates to getUpdates while a webhook is set,
    and aiogram's start_polling never removes one. A webhook left behind by an
    earlier RUN_MODE=webhook run is therefore a *silent* bot: it receives
    nothing at all and answers nothing.
    """
    try:
        info = await bot.get_webhook_info()
    except Exception:
        log.exception("could not inspect the webhook of the %s bot", name)
        return

    if not info.url:
        log.info("%s: no webhook registered - long polling can receive updates", name)
        return

    log.warning(
        "%s still has a webhook registered (%s, pending=%s, last error=%s) - "
        "removing it, otherwise long polling receives NOTHING",
        name, info.url, info.pending_update_count, info.last_error_message or "-",
    )
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        log.warning("%s: webhook deleted - long polling is now active", name)
    except Exception:
        log.exception("failed to delete the webhook of the %s bot", name)


async def _run_polling(bot_master: Bot, dp_master: Dispatcher,
                       bot_worker: Bot, dp_worker: Dispatcher) -> None:
    await _clear_stale_webhook(bot_master, "master")
    await _clear_stale_webhook(bot_worker, "worker")

    stop = await _install_stop_signal()

    master_updates = allowed_updates(dp_master, require_pv=True)
    worker_updates = allowed_updates(dp_worker)
    log.info(
        "polling allowed_updates: master=%s worker=%s", master_updates, worker_updates
    )

    tasks = [
        asyncio.create_task(
            dp_master.start_polling(
                bot_master,
                allowed_updates=master_updates,
                handle_signals=False,
                handle_as_tasks=True,
            ),
            name="poll-master",
        ),
        asyncio.create_task(
            dp_worker.start_polling(
                bot_worker,
                allowed_updates=worker_updates,
                handle_signals=False,
                handle_as_tasks=True,
            ),
            name="poll-worker",
        ),
    ]
    log.info("dual-bot long polling started (master + worker)")

    await stop.wait()
    log.info("stop signal received - winding down")

    for dp in (dp_master, dp_worker):
        with suppress(Exception):
            await dp.stop_polling()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# Mode B - webhooks behind a reverse proxy (two paths, one aiohttp app)
# ---------------------------------------------------------------------------
async def _run_webhook(bot_master: Bot, dp_master: Dispatcher,
                       bot_worker: Bot, dp_worker: Dispatcher) -> None:
    app = web.Application()

    SimpleRequestHandler(
        dispatcher=dp_master, bot=bot_master, handle_in_background=True
    ).register(app, path=settings.webhook_path_master)
    SimpleRequestHandler(
        dispatcher=dp_worker, bot=bot_worker, handle_in_background=True
    ).register(app, path=settings.webhook_path_worker)

    setup_application(app, dispatcher=dp_master, bot=bot_master)
    setup_application(app, dispatcher=dp_worker, bot=bot_worker)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", settings.webhook_port).start()

    url_master = f"{settings.webhook_host}{settings.webhook_path_master}"
    url_worker = f"{settings.webhook_host}{settings.webhook_path_worker}"

    master_updates = allowed_updates(dp_master, require_pv=True)
    worker_updates = allowed_updates(dp_worker)
    log.info(
        "webhook allowed_updates: master=%s worker=%s", master_updates, worker_updates
    )

    await bot_master.set_webhook(
        url=url_master,
        allowed_updates=master_updates,
        max_connections=64,
        drop_pending_updates=False,
    )
    await bot_worker.set_webhook(
        url=url_worker,
        allowed_updates=worker_updates,
        max_connections=64,
        drop_pending_updates=False,
    )
    log.info("webhooks registered: %s | %s", url_master, url_worker)

    stop = await _install_stop_signal()
    await stop.wait()

    with suppress(Exception):
        await bot_master.set_webhook(url="")
    with suppress(Exception):
        await bot_worker.set_webhook(url="")
    await runner.cleanup()


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
async def main() -> None:
    setup_logging()
    log.info("ABG Guard booting (mode=%s)", settings.run_mode)
    log.info("Level 3 (God Admin) ids: %s", sorted(settings.god_admin_ids))

    await init_db()
    await rbac.bootstrap()
    await harvester.start()

    bot_master, dp_master = create_master(settings.master_token)
    bot_worker, dp_worker = create_worker(settings.worker_token)

    me = await bot_master.me()
    worker_me = await bot_worker.me()
    log.info(
        "master=@%s id=%s | worker=@%s id=%s",
        me.username, me.id, worker_me.username, worker_me.id,
    )
    log.info(
        "PV dashboard (/start, /panel, the «پنل» trigger and /help) is served by "
        "@%s only - @%s is media-only and never answers in PV",
        me.username, worker_me.username,
    )
    log.info(
        "update intake: %s",
        "webhook" if settings.run_mode == "webhook" else "long polling (no webhook)",
    )

    bus = BanEventBus(bot_master, me.id)
    await bus.start()

    runner: Callable[[], Awaitable[None]] = (
        _run_webhook if settings.run_mode == "webhook" else _run_polling
    )

    try:
        await runner(bot_master, dp_master, bot_worker, dp_worker)
    finally:
        log.info("shutting down services")
        await bus.stop()
        await harvester.stop()
        await porn_client.close()
        with suppress(Exception):
            await bot_master.session.close()
        with suppress(Exception):
            await bot_worker.session.close()
        await close_db()
        await close_redis()
        log.info("ABG Guard stopped. harvester stats=%s", harvester.stats)
