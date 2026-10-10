from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram import Bot

from abgguard.core import redis as rk
from abgguard.services.moderation import handle_ban_event

log = logging.getLogger("abgguard.bus")


class BanEventBus:
    """
    Master-side consumer of ban events.

    The Worker Bot (and the Master itself) push
    ``{"chat_id", "user_id", "reason", ...}`` onto the durable Redis list
    ``abg:events:ban:q`` right after offending media has been deleted.
    This task BRPOPs the queue and executes the real Telegram ban with the
    Master Bot, then writes an audit row into PostgreSQL.

    A list (rather than pub/sub) is used on purpose: events survive a master
    restart, and there is no subscribe-before-publish race.
    """

    def __init__(self, bot: Bot, me_id: int) -> None:
        self.bot = bot
        self.me_id = me_id
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._ready: asyncio.Event | None = None

    async def start(self) -> None:
        """Start consuming and wait until the queue is actually attached."""
        self._ready = asyncio.Event()
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="ban-event-bus")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=10)
        except TimeoutError:
            log.warning("ban event bus did not signal readiness within 10s")

    async def stop(self, grace: float = 5.0) -> None:
        """
        Drain gracefully: finish the event currently being processed (ban +
        audit row) before the task is torn down, so nothing is half-written.
        """
        self._stopping.set()
        task, self._task = self._task, None
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=grace)
        except TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        except (asyncio.CancelledError, Exception):
            pass

    async def _run(self) -> None:
        rclient = rk.get_redis()
        try:
            # warm the connection up so no event can be produced before we listen
            await rclient.ping()
            log.info("ban event bus draining %s", rk.BAN_QUEUE)
            if self._ready:
                self._ready.set()

            while not self._stopping.is_set():
                item: Any = await rclient.brpop(rk.BAN_QUEUE, timeout=1)
                if item is None:
                    continue
                _key, data = item
                if isinstance(data, bytes):
                    data = data.decode("utf-8", "replace")
                try:
                    await handle_ban_event(self.bot, data, self.me_id)
                except Exception:
                    log.exception("ban event handling failed: %s", str(data)[:200])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ban event bus crashed")
            if self._ready:
                self._ready.set()
        finally:
            log.info("ban event bus stopped")
