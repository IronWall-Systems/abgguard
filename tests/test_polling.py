from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from aiogram import Bot

from abgguard.app import _clear_stale_webhook


def _info(url: str):
    async def _get(self):
        return SimpleNamespace(
            url=url, pending_update_count=7, last_error_message=None
        )

    return _get


def test_a_stale_webhook_is_removed_before_polling(master, monkeypatch, caplog) -> None:
    caplog.set_level(logging.WARNING)
    bot, _ = master
    deleted: list[bool] = []

    async def _delete(self, drop_pending_updates: bool = True, **kwargs):
        deleted.append(drop_pending_updates)

    monkeypatch.setattr(Bot, "get_webhook_info", _info("https://api.tg.example/tg/master"))
    monkeypatch.setattr(Bot, "delete_webhook", _delete)

    asyncio.run(_clear_stale_webhook(bot, "master"))

    assert deleted == [False]
    assert any("long polling receives NOTHING" in r.message for r in caplog.records)


def test_a_clean_bot_is_left_alone(master, monkeypatch) -> None:
    bot, _ = master
    deleted: list[bool] = []

    async def _delete(self, **kwargs):
        deleted.append(True)

    monkeypatch.setattr(Bot, "get_webhook_info", _info(""))
    monkeypatch.setattr(Bot, "delete_webhook", _delete)

    asyncio.run(_clear_stale_webhook(bot, "master"))

    assert deleted == [], "a bot without a webhook must not be touched"
