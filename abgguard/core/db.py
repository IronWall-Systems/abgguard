from __future__ import annotations

import logging

from tortoise import Tortoise

from abgguard.config import settings

log = logging.getLogger("abgguard.db")

# single source of truth - used by the runtime and by `aerich`
TORTOISE_ORM = {
    "connections": {"default": settings.postgres_dsn},
    "apps": {
        "models": {
            "models": ["abgguard.models.entities"],
            "default_connection": "default",
        }
    },
}


async def init_db() -> None:
    await Tortoise.init(config=TORTOISE_ORM)
    await Tortoise.generate_schemas(safe=True)
    await _ensure_trigger_column()
    log.info("PostgreSQL (Tortoise ORM) ready")


async def _ensure_trigger_column() -> None:
    """`generate_schemas(safe=True)` never ALTERs an existing table.

    The per-group panel trigger was added after the `groups` table was first
    deployed, so the column is created here - idempotently, on every boot.
    """
    ddl = (
        "ALTER TABLE groups "
        "ADD COLUMN IF NOT EXISTS trigger_name VARCHAR(32) "
        "NOT NULL DEFAULT 'پنل'"
    )
    try:
        conn = Tortoise.get_connection("default")
        await conn.execute_query(ddl)
    except Exception:
        # an unsupported backend / read-only role must not block boot: the
        # trigger store falls back to the default trigger name instead
        log.warning("could not ensure groups.trigger_name column", exc_info=True)


async def close_db() -> None:
    await Tortoise.close_connections()
