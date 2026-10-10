from __future__ import annotations

import asyncio

from abgguard.master.handlers import pv_panel
from abgguard.services import metrics


class _Pool:
    def get_min_size(self) -> int:
        return 2

    def get_max_size(self) -> int:
        return 10

    def get_size(self) -> int:
        return 4

    def get_idle_size(self) -> int:
        return 3


class _Client:
    """Tortoise 0.24's asyncpg client shape: no ``is_connected`` anywhere."""

    def __init__(self, *, fail: bool = False, is_connected: object = None) -> None:
        self._pool = _Pool()
        self.fail = fail
        self.probes = 0
        if is_connected is not None:
            self.is_connected = is_connected

    async def execute_query(self, query: str, values=None):
        self.probes += 1
        if self.fail:
            raise ConnectionError("server closed the connection")
        return 1, [1]


async def _counts() -> dict[str, int]:
    return {"groups": 0, "members": 0, "actions": 0, "mutes": 0, "bans": 0, "owners": 0}


def test_live_orm_client_is_reported_connected(monkeypatch) -> None:
    client = _Client()
    monkeypatch.setattr(metrics, "_default_db_client", lambda: client)

    stats = asyncio.run(metrics.pg_pool_stats())

    assert stats["connected"] is True
    assert client.probes == 1, "a client without is_connected must be probed"
    assert (stats["min"], stats["max"], stats["open"], stats["idle"]) == (2, 10, 4, 3)


def test_is_connected_short_circuits_the_probe(monkeypatch) -> None:
    client = _Client(is_connected=lambda: True)
    monkeypatch.setattr(metrics, "_default_db_client", lambda: client)

    stats = asyncio.run(metrics.pg_pool_stats())

    assert stats["connected"] is True
    assert client.probes == 0


def test_a_failing_probe_marks_the_pool_cut(monkeypatch) -> None:
    client = _Client(fail=True)
    monkeypatch.setattr(metrics, "_default_db_client", lambda: client)

    stats = asyncio.run(metrics.pg_pool_stats())

    assert stats["connected"] is False
    assert "status" not in stats, "the pool exists, only the server is unreachable"


def test_an_idle_orm_is_reported_as_not_connected() -> None:
    assert metrics._default_db_client() is None

    stats = asyncio.run(metrics.pg_pool_stats())

    assert stats == {"connected": False, "status": "no connection"}


def test_panel_status_turns_green_again(monkeypatch) -> None:
    async def _snapshot():
        return {
            "cpu_percent": 1.0,
            "pg": {"connected": True, "open": 4, "max": 10},
            "redis_ms": 1,
        }

    monkeypatch.setattr(pv_panel.metrics, "system_snapshot", _snapshot)
    monkeypatch.setattr(pv_panel, "_global_counts", _counts)

    text = asyncio.run(pv_panel._status_text())

    assert "PostgreSQL: 🟢 متصل (open 4 / max 10)" in text
    assert "🔴 قطع" not in text


def test_panel_status_stays_red_only_when_the_probe_fails(monkeypatch) -> None:
    async def _snapshot():
        return {"cpu_percent": 1.0, "pg": {"connected": False}, "redis_ms": 1}

    monkeypatch.setattr(pv_panel.metrics, "system_snapshot", _snapshot)
    monkeypatch.setattr(pv_panel, "_global_counts", _counts)

    text = asyncio.run(pv_panel._status_text())

    assert "PostgreSQL: 🔴 قطع" in text
    assert "🟢" not in text
