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


def _first_env(*names: str, default: str | None = None) -> str | None:
    """First non-empty variable among aliases (e.g. ABG_BOT_TOKEN/MASTER_BOT_TOKEN)."""
    for name in names:
        value = _env(name)
        if value:
            return value
    return default


def _csv_ints(raw: str | None) -> tuple[int, ...]:
    if not raw:
        return ()
    out: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.isdigit() or (chunk.startswith("-") and chunk[1:].isdigit()):
            out.append(int(chunk))
    return tuple(out)


def _porn_api_url() -> str:
    raw = _first_env(
        "PORN_API_URL", "PORNSCAN_URL", default="http://127.0.0.1:8010/detect"
    )
    assert raw is not None
    raw = raw.strip().rstrip("/")
    return raw if raw.endswith("/detect") else f"{raw}/detect"


def _bot_api_base() -> str:
    """Local Bot API server base URL; empty string falls back to the cloud API."""
    raw = _first_env(
        "TELEGRAM_BOT_API_URL",
        "LOCAL_BOT_API_URL",
        "BOT_API_URL",
        default="http://127.0.0.1:8081",
    )
    assert raw is not None
    raw = raw.strip().rstrip("/")
    if raw.lower() in {"off", "cloud", "none", "disabled"}:
        return ""
    return raw


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
    captcha_timeout: int = 300
    broadcast_delay: float = 0.05

    # Local Telegram Bot API server (enables >48h deletions / bulk purge).
    # Set TELEGRAM_BOT_API_URL=off to fall back to the cloud API.
    bot_api_base: str = "http://127.0.0.1:8081"
    # Paths used to read media files directly in --local mode.  The server
    # reports container paths under bot_api_server_dir; the bot maps them to
    # the host bind mount in bot_api_data_dir before downloading.
    bot_api_server_dir: Path = Path("/var/lib/telegram-bot-api")
    bot_api_data_dir: Path = Path("/opt/dailydev-telegram-bot/telegram-bot-api-data")

    # SaaS subscription engine
    price_toman: int = 750_000
    price_usd: int = 3
    cycle_days: int = 30
    trial_hours: int = 24
    trial_min_members: int = 50
    warn_days: tuple[int, ...] = (3, 2, 1)
    payment_card_number: str = ""
    payment_card_holder: str = ""
    payment_crypto_address: str = ""

    # dual-bot distributed fallback (master heartbeat, worker watchdog)
    master_username: str = "abgguardbot"
    worker_username: str = "ABGGUARD1BOT"
    heartbeat_ttl: int = 45
    heartbeat_interval: int = 15

    redis_prefix: str = field(default="abg")

    @property
    def god_admin_id(self) -> int:
        """Primary Level 3 recipient (receipts, approvals, private mod-log).

        The Master bot talks to this id only; the worker/media bot never
        forwards admin notifications.
        """
        return self.god_admin_ids[0] if self.god_admin_ids else FALLBACK_GOD_ADMIN_ID

    @classmethod
    def from_env(cls) -> Settings:
        master_token = _first_env("MASTER_BOT_TOKEN", "ABG_BOT_TOKEN")
        worker_token = _first_env("WORKER_BOT_TOKEN")
        missing = [
            name
            for name, value in (
                ("ABG_BOT_TOKEN/MASTER_BOT_TOKEN", master_token),
                ("WORKER_BOT_TOKEN", worker_token),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

        return cls(
            master_token=str(master_token),
            worker_token=str(worker_token),
            god_admin_ids=_god_admin_ids(),
            postgres_dsn=_postgres_dsn(),
            redis_dsn=_redis_dsn(),
            porn_api_url=_porn_api_url(),
            porn_api_timeout=_f("PORN_API_TIMEOUT", 30.0),
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
            bot_api_base=_bot_api_base(),
            bot_api_server_dir=Path(
                _env("TELEGRAM_BOT_API_SERVER_DIR", "/var/lib/telegram-bot-api")
            ),
            bot_api_data_dir=Path(
                _env(
                    "TELEGRAM_BOT_API_DATA_DIR",
                    "/opt/dailydev-telegram-bot/telegram-bot-api-data",
                )
            ),
            price_toman=_i("PRICE_TOMAN", 750_000),
            price_usd=_i("PRICE_USD", 3),
            cycle_days=_i("CYCLE_DAYS", 30),
            trial_hours=_i("TRIAL_HOURS", 24),
            trial_min_members=_i("TRIAL_MIN_MEMBERS", 50),
            warn_days=_csv_ints(_env("WARN_DAYS")) or (3, 2, 1),
            payment_card_number=str(_env("PAYMENT_CARD_NUMBER") or ""),
            payment_card_holder=str(_env("PAYMENT_CARD_HOLDER") or ""),
            payment_crypto_address=str(_env("PAYMENT_CRYPTO_ADDRESS") or ""),
            master_username=str(_env("MASTER_USERNAME") or "abgguardbot").lstrip("@"),
            worker_username=str(_env("WORKER_USERNAME") or "ABGGUARD1BOT").lstrip("@"),
            heartbeat_ttl=_i("HEARTBEAT_TTL", 45),
            heartbeat_interval=_i("HEARTBEAT_INTERVAL", 15),
        )


settings = Settings.from_env()
