from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("abgguard.config")

# A broken / empty .env must never lock the operator out of the dashboard:
# this account is always Level 3, whatever the environment says.
FALLBACK_GOD_ADMIN_ID = 6029973681


def _env(name: str, default: str | None = None) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw if raw else default


def _csv_ints(raw: str | None) -> tuple[int, ...]:
    if not raw:
        return ()
    out: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.isdigit() or (chunk.startswith("-") and chunk[1:].isdigit()):
            out.append(int(chunk))
    return tuple(out)


def _f(name: str, default: float) -> float:
    return float(_env(name, str(default)))  # type: ignore[arg-type]


def _i(name: str, default: int) -> int:
    return int(_env(name, str(default)))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# connection string builders - accept both the exploded and the DSN form
# ---------------------------------------------------------------------------
def _postgres_dsn() -> str:
    """
    POSTGRES_DSN wins if present, otherwise the connection is assembled from
    POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB / POSTGRES_HOST / POSTGRES_PORT.
    """
    explicit = _env("POSTGRES_DSN")
    if explicit:
        return explicit

    user = _env("POSTGRES_USER", "abg")
    password = _env("POSTGRES_PASSWORD", "abg")
    database = _env("POSTGRES_DB", "abgguard")
    host = _env("POSTGRES_HOST", "127.0.0.1")
    port = _env("POSTGRES_PORT", "5432")
    return (
        f"postgres://{quote_plus(str(user))}:{quote_plus(str(password))}"
        f"@{host}:{port}/{database}"
    )


def _redis_dsn() -> str:
    return (
        _env("REDIS_DSN")
        or _env("REDIS_URL")
        or "redis://127.0.0.1:6379/0"
    )


def _god_admin_ids() -> tuple[int, ...]:
    """Accepts GOD_ADMIN_IDS=1,2,3 or the single-valued GOD_ADMIN_ID=1.

    Falls back to the built-in Level 3 id when the variable is missing,
    empty or unparsable - a broken env must not revoke God Admin access.
    """
    ids = _csv_ints(_env("GOD_ADMIN_IDS") or _env("GOD_ADMIN_ID"))
    if ids:
        return ids
    log.error(
        "GOD_ADMIN_IDS/GOD_ADMIN_ID is missing or unparsable - "
        "falling back to the built-in God Admin id %s",
        FALLBACK_GOD_ADMIN_ID,
    )
    return (FALLBACK_GOD_ADMIN_ID,)


@dataclass(slots=True, frozen=True)
class Settings:
    master_token: str
    worker_token: str
    god_admin_ids: tuple[int, ...]

    postgres_dsn: str
    redis_dsn: str

    porn_api_url: str
    porn_api_timeout: float
    porn_threshold: float

    dataset_dir: Path
    dataset_file: Path
    dataset_max_bytes: int
    dataset_flush_interval: float

    flood_text_limit: int
    flood_text_window: int
    flood_join_limit: int
    flood_join_window: int
    join_lock_seconds: int
    join_slow_mode: int

    run_mode: str
    webhook_host: str
    webhook_path_master: str
    webhook_path_worker: str
    webhook_port: int
    log_level: str

    webapp_url: str = ""
    support_url: str = ""
    captcha_timeout: int = 60
    broadcast_delay: float = 0.05

    redis_prefix: str = field(default="abg")

    @classmethod
    def from_env(cls) -> Settings:
        missing = [
            k for k in ("MASTER_BOT_TOKEN", "WORKER_BOT_TOKEN") if not _env(k)
        ]
        if missing:
            raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

        return cls(
            master_token=str(_env("MASTER_BOT_TOKEN")),
            worker_token=str(_env("WORKER_BOT_TOKEN")),
            god_admin_ids=_god_admin_ids(),
            postgres_dsn=_postgres_dsn(),
            redis_dsn=_redis_dsn(),
            porn_api_url=_env("PORN_API_URL", "http://127.0.0.1:8010/detect"),  # type: ignore[arg-type]
            porn_api_timeout=_f("PORN_API_TIMEOUT", 6.0),
            porn_threshold=_f("PORN_THRESHOLD", 0.55),
            dataset_dir=Path(_env("DATASET_DIR", "./dataset")),  # type: ignore[arg-type]
            dataset_file=Path(_env("DATASET_FILE", "./data/genz_corpus.jsonl")),  # type: ignore[arg-type]
            dataset_max_bytes=_i("DATASET_MAX_BYTES", 50 * 1024 * 1024),
            dataset_flush_interval=_f("DATASET_FLUSH_INTERVAL", 2.0),
            flood_text_limit=_i("FLOOD_TEXT_LIMIT", 5),
            flood_text_window=_i("FLOOD_TEXT_WINDOW", 3),
            flood_join_limit=_i("FLOOD_JOIN_LIMIT", 100),
            flood_join_window=_i("FLOOD_JOIN_WINDOW", 60),
            join_lock_seconds=_i("JOIN_LOCK_SECONDS", 60),
            join_slow_mode=_i("JOIN_SLOW_MODE", 30),
            run_mode=str(_env("RUN_MODE", "polling")).lower(),
            webhook_host=str(_env("WEBHOOK_HOST", "")).rstrip("/"),
            webhook_path_master=_env("WEBHOOK_PATH_MASTER", "/tg/master"),  # type: ignore[arg-type]
            webhook_path_worker=_env("WEBHOOK_PATH_WORKER", "/tg/worker"),  # type: ignore[arg-type]
            webhook_port=_i("WEBHOOK_PORT", 8443),
            log_level=str(_env("LOG_LEVEL", "INFO")).upper(),
            webapp_url=str(_env("DASHBOARD_WEBAPP_URL", "")).rstrip("/"),
            support_url=str(_env("SUPPORT_CHANNEL_URL", "")).rstrip("/"),
            captcha_timeout=_i("CAPTCHA_TIMEOUT", 60),
            broadcast_delay=_f("BROADCAST_DELAY", 0.05),
        )


settings = Settings.from_env()
