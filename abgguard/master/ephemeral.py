from __future__ import annotations

import asyncio
import logging

from aiogram.types import Message

log = logging.getLogger("abgguard.ephemeral")

# strong references so fire-and-forget cleanups are never garbage collected
_BACKGROUND: set[asyncio.Task] = set()

_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa_num(value: int | str) -> str:
    """Latin digits -> Persian digits (۰۱۲۳۴۵۶۷۸۹)."""
    return str(value).translate(_PERSIAN_DIGITS)


def spawn(coro, *, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return task


async def delete_later(message: Message, delay: float) -> None:
    """Self-destruct a message after `delay` seconds (project rule 6)."""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception as exc:
        log.debug("ephemeral delete skipped: %s", exc)
    else:
        log.info(
            "DELETE (ephemeral %.0fs) chat=%s msg=%s",
            delay,
            message.chat.id,
            message.message_id,
        )


async def notice(message: Message, text: str, *, delay: float = 10.0) -> None:
    """Send a short reply that cleans itself up so the chat stays untouched."""
    try:
        sent = await message.answer(text)
    except Exception as exc:
        log.debug("ephemeral notice failed: %s", exc)
        return
    spawn(delete_later(sent, delay), name="ephemeral-notice")
