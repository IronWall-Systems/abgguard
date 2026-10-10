from __future__ import annotations

import os

# abgguard.config reads the environment at import time - seed it before any
# project module is imported so the suite runs on a bare machine (load_dotenv
# never overrides variables that are already set).
os.environ["MASTER_BOT_TOKEN"] = "1:test-token"
os.environ["WORKER_BOT_TOKEN"] = "2:test-token"
os.environ["GOD_ADMIN_ID"] = "1"
os.environ["POSTGRES_DSN"] = "postgres://user:pass@127.0.0.1:5432/abgguard"
os.environ["REDIS_URL"] = "redis://127.0.0.1:6379/15"
os.environ["SUPPORT_CHANNEL_URL"] = "https://t.me/abg_support"

import asyncio

import pytest

TOKEN = "1:test-token"


@pytest.fixture(autouse=True)
def _stub_fsm_storage(monkeypatch):
    """
    The FSM middleware reads the state on every update. Redis is not part of
    the unit-test contract, so the storage is stubbed out for the whole suite.
    """
    from aiogram.fsm.storage.redis import RedisStorage

    async def _none(*args, **kwargs):
        return None

    async def _empty(*args, **kwargs):
        return {}

    monkeypatch.setattr(RedisStorage, "get_state", _none)
    monkeypatch.setattr(RedisStorage, "set_state", _none)
    monkeypatch.setattr(RedisStorage, "get_data", _empty)
    monkeypatch.setattr(RedisStorage, "set_data", _none)
    monkeypatch.setattr(RedisStorage, "update_data", _none)
    monkeypatch.setattr(RedisStorage, "close", _none)


@pytest.fixture(scope="session")
def master():
    """Built once: aiogram routers are module-level singletons."""
    from abgguard.master.bot import create_master

    bot, dp = create_master(TOKEN)
    yield bot, dp
    asyncio.run(bot.session.close())


@pytest.fixture(scope="session")
def worker():
    from abgguard.worker.bot import create_worker

    bot, dp = create_worker(TOKEN)
    yield bot, dp
    asyncio.run(bot.session.close())
