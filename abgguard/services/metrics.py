from __future__ import annotations

import asyncio
import http.client
import inspect
import json
import logging
import os
import socket
import time
from pathlib import Path

from abgguard.config import settings
from abgguard.core import redis as rk

log = logging.getLogger("abgguard.metrics")

MB = 1024 * 1024
CG_V2 = Path("/sys/fs/cgroup")
CG_V1 = Path("/sys/fs/cgroup/memory")
DOCKER_SOCK = Path("/var/run/docker.sock")

# services of this compose stack - only these containers are ever queried
DOCKER_SERVICES = ("abgguard", "master_bot", "worker_bot", "postgres", "redis", "pornscan")
DOCKER_TIMEOUT = 2.0  # seconds per request - the dashboard must never hang
PG_PROBE_TIMEOUT = 2.0  # seconds - a dead server must never stall the dashboard


# --------------------------------------------------------------------------
# CPU / RAM  (no psutil: /proc + cgroup are dependency free and 1 CPU safe)
# --------------------------------------------------------------------------
def cpu_percent() -> float | None:
    """1-minute load average normalised by the core count (container wide)."""
    try:
        one = os.getloadavg()[0]
    except OSError:  # pragma: no cover - platform without /proc/loadavg
        return None
    cores = os.cpu_count() or 1
    return round(min(100.0, (one / cores) * 100.0), 1)


def _read_int(path: Path) -> int | None:
    try:
        raw = path.read_text().split()[0]
    except (OSError, IndexError, ValueError):
        return None
    if raw == "max":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def host_memory() -> dict[str, int | float] | None:
    """Host-wide totals from /proc/meminfo (kB -> bytes)."""
    try:
        lines = Path("/proc/meminfo").read_text().splitlines()
    except OSError:  # pragma: no cover
        return None
    values: dict[str, int] = {}
    for line in lines:
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[key] = int(parts[0]) * 1024
    total = values.get("MemTotal")
    if not total:
        return None
    available = values.get("MemAvailable", values.get("MemFree", 0))
    used = max(0, total - available)
    return {"total": total, "used": used, "percent": round(used / total * 100, 1)}


def cgroup_memory() -> dict[str, int | float] | None:
    """This container's own usage against the compose `memory:` hard cap."""
    current = _read_int(CG_V2 / "memory.current")
    limit = _read_int(CG_V2 / "memory.max")
    if current is None:
        current = _read_int(CG_V1 / "memory.usage_in_bytes")
        limit = _read_int(CG_V1 / "memory.limit_in_bytes")
        if limit is not None and limit > 1 << 50:  # "unlimited" v1 sentinel
            limit = None
    if current is None:
        return None
    out: dict[str, int | float] = {"used": current, "limit": limit}
    out["percent"] = round(current / limit * 100, 1) if limit else None
    return out


# --------------------------------------------------------------------------
# Redis / PostgreSQL
# --------------------------------------------------------------------------
async def redis_ping_ms() -> float | None:
    start = time.perf_counter()
    try:
        await rk.get_redis().ping()
    except Exception:
        log.debug("redis ping failed", exc_info=True)
        return None
    return round((time.perf_counter() - start) * 1000, 2)


async def pg_pool_stats() -> dict[str, int | bool | str]:
    """Best-effort view of the Tortoise/asyncpg connection pool.

    ``connected`` must be answered by a live round-trip whenever the ORM cannot
    answer it itself: Tortoise 0.24 ships no ``is_connected`` and no
    ``Tortoise.get_connections()``, so trusting either makes a perfectly healthy
    PostgreSQL look cut in the dashboard.
    """
    client = _default_db_client()
    if client is None:
        return {"connected": False, "status": "no connection"}

    out: dict[str, int | bool | str] = {}
    try:
        out["connected"] = await _pg_connected(client)
        pool = getattr(client, "_pool", None) or getattr(client, "pool", None)
        if pool is not None:
            for key, method in (
                ("min", "get_min_size"),
                ("max", "get_max_size"),
                ("open", "get_size"),
                ("idle", "get_idle_size"),
            ):
                fn = getattr(pool, method, None)
                if callable(fn):
                    try:
                        value = fn()
                        if inspect.isawaitable(value):
                            value = await value
                        out[key] = int(value)
                    except Exception:  # pragma: no cover - pool internals
                        pass
    except Exception:
        log.debug("pg pool stats unavailable", exc_info=True)
        out["status"] = "n/a"
        out["connected"] = False
    return out


def _default_db_client() -> object | None:
    """The ``default`` Tortoise client, or ``None`` when the ORM is idle."""
    from tortoise import Tortoise

    try:
        getter = getattr(Tortoise, "get_connections", None)
        if callable(getter):
            conns = getter()
            return conns.get("default") if isinstance(conns, dict) else None
        return Tortoise.get_connection("default")
    except Exception:
        log.debug("no default tortoise connection", exc_info=True)
        return None


async def _pg_connected(client: object) -> bool:
    """True when PostgreSQL answers - a probe, not a guess."""
    flag = getattr(client, "is_connected", None)
    try:
        if flag is not None and (flag() if callable(flag) else flag):
            return True
    except Exception:  # pragma: no cover - fall through to the probe
        log.debug("is_connected failed", exc_info=True)
    try:
        await asyncio.wait_for(client.execute_query("SELECT 1"), PG_PROBE_TIMEOUT)
        return True
    except Exception:
        log.debug("postgres probe failed", exc_info=True)
        return False


# --------------------------------------------------------------------------
# Docker engine (optional - only when /var/run/docker.sock is mounted)
# --------------------------------------------------------------------------
class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: Path, timeout: float = DOCKER_TIMEOUT) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = str(path)

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


def _docker_json(path: str) -> dict | list | None:
    conn = _UnixHTTPConnection(DOCKER_SOCK)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        if resp.status != 200:
            return None
        return json.loads(resp.read())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    finally:
        conn.close()


def _container_cpu(stats: dict) -> float | None:
    cur = stats.get("cpu_stats") or {}
    pre = stats.get("precpu_stats") or {}
    cpu_total = ((cur.get("cpu_usage") or {}).get("total_usage", 0)
                 - (pre.get("cpu_usage") or {}).get("total_usage", 0))
    system = (cur.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0))
    cpus = len(cur.get("percpu_usage") or []) or cur.get("online_cpus") or 1
    if cpu_total <= 0 or system <= 0:
        return 0.0
    return round(cpu_total / system * cpus * 100, 1)


def _docker_stats_sync() -> list[dict] | None:
    listing = _docker_json("/containers/json")
    if not isinstance(listing, list):
        return None
    targets = []
    for item in listing:
        labels = item.get("Labels") or {}
        service = labels.get("com.docker.compose.service", "")
        name = (item.get("Names") or [""])[0].lstrip("/")
        if service in DOCKER_SERVICES or any(s in name for s in DOCKER_SERVICES):
            targets.append((name, item.get("Id", "")))
    if not targets:
        return None

    out: list[dict] = []
    for name, cid in targets:
        stats = _docker_json(f"/containers/{cid}/stats?stream=false&one-shot=true")
        if not isinstance(stats, dict):
            continue
        mem = stats.get("memory_stats") or {}
        out.append(
            {
                "name": name,
                "cpu": _container_cpu(stats),
                "mem_used": int(mem.get("usage", 0)),
                "mem_limit": int(mem.get("limit", 0)),
            }
        )
    return out or None


async def docker_stats() -> list[dict] | None:
    if not DOCKER_SOCK.exists():
        return None
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_docker_stats_sync), timeout=DOCKER_TIMEOUT * 5
        )
    except Exception:
        log.debug("docker stats unavailable", exc_info=True)
        return None


# --------------------------------------------------------------------------
# ML dataset harvester
# --------------------------------------------------------------------------
def dataset_stats() -> dict[str, int | float]:
    from abgguard.services.harvester import harvester

    stats = dict(harvester.stats)
    archives: list[Path] = []
    try:
        archives = list(settings.dataset_dir.glob("*.zip"))
    except OSError:  # pragma: no cover
        pass
    stats["archives"] = len(archives)
    stats["archive_mb"] = round(
        sum(p.stat().st_size for p in archives if p.is_file()) / MB, 2
    )
    stats["max_mb"] = round(settings.dataset_max_bytes / MB, 1)
    return stats


# --------------------------------------------------------------------------
# aggregate
# --------------------------------------------------------------------------
async def system_snapshot() -> dict:
    cpu, host, cg, containers = await asyncio.gather(
        asyncio.to_thread(cpu_percent),
        asyncio.to_thread(host_memory),
        asyncio.to_thread(cgroup_memory),
        docker_stats(),
    )
    return {
        "cpu_percent": cpu,
        "host": host,
        "cgroup": cg,
        "containers": containers,
        "redis_ms": await redis_ping_ms(),
        "pg": await pg_pool_stats(),
        "dataset": await asyncio.to_thread(dataset_stats),
    }


def fmt_mb(value: int | float | None) -> str:
    if value is None:
        return "-"
    return f"{value / MB:.1f} MB"
