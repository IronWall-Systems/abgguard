from __future__ import annotations

import redis.asyncio as aioredis

from abgguard.config import settings

P = settings.redis_prefix

# ---- key builders -------------------------------------------------------
RBAC_GOD = f"{P}:rbac:god"
RBAC_ADMIN = f"{P}:rbac:admin"          # + :{chat_id}   -> set(user_id)
RBAC_SUB = f"{P}:rbac:sub"              # + :{chat_id}   -> set(user_id)
SETTINGS = f"{P}:settings"              # + :{chat_id}   -> hash
BADWORDS = f"{P}:bad"                   # + :{chat_id}   -> set(word)
FLOOD_TEXT = f"{P}:flood:text"          # + :{chat}:{uid}
FLOOD_TEXT_HASH = f"{P}:flood:thash"    # + :{chat}:{uid}
FLOOD_JOIN = f"{P}:flood:join"          # + :{chat}
JOIN_LOCK = f"{P}:lock:join"            # + :{chat}
BAN_QUEUE = f"{P}:events:ban:q"          # durable RPUSH/BRPOP queue
BAN_CHANNEL = f"{P}:events:ban"          # pub/sub fan-out (kept for consumers)
PORN_FLAG = f"{P}:flag:porn"            # + :{chat}:{msg}
RBAC_CACHE = f"{P}:rbac:cache"          # + :{chat}:{uid} -> int role
POLICY = f"{P}:policy"                  # + :{chat} -> hash(captcha, welcome)
GROUP = f"{P}:group"                    # + :{chat}:trigger_name -> str
STATS_MEMBERS = f"{P}:stats:members"    # HyperLogLog of protected members
STATS_SEEN = f"{P}:stats:seen"          # + :{chat} -> HyperLogLog per group


def k_admin(chat_id: int) -> str:
    return f"{RBAC_ADMIN}:{chat_id}"


def k_sub(chat_id: int) -> str:
    return f"{RBAC_SUB}:{chat_id}"


def k_settings(chat_id: int) -> str:
    return f"{SETTINGS}:{chat_id}"


def k_badwords(chat_id: int) -> str:
    return f"{BADWORDS}:{chat_id}"


def k_flood_text(chat_id: int, user_id: int) -> str:
    return f"{FLOOD_TEXT}:{chat_id}:{user_id}"


def k_flood_hash(chat_id: int, user_id: int) -> str:
    return f"{FLOOD_TEXT_HASH}:{chat_id}:{user_id}"


def k_flood_join(chat_id: int) -> str:
    return f"{FLOOD_JOIN}:{chat_id}"


def k_join_lock(chat_id: int) -> str:
    return f"{JOIN_LOCK}:{chat_id}"


def k_porn_flag(chat_id: int, message_id: int) -> str:
    return f"{PORN_FLAG}:{chat_id}:{message_id}"


def k_rbac_cache(chat_id: int, user_id: int) -> str:
    return f"{RBAC_CACHE}:{chat_id}:{user_id}"


def k_policy(chat_id: int) -> str:
    return f"{POLICY}:{chat_id}"


def k_group_trigger(chat_id: int) -> str:
    """abg:group:{chat_id}:trigger_name -> the per-group panel alias."""
    return f"{GROUP}:{chat_id}:trigger_name"


def k_stats_seen(chat_id: int) -> str:
    return f"{STATS_SEEN}:{chat_id}"


def k_user_vip(chat_id: int, user_id: int) -> str:
    """abg:group:{chat_id}:vip:{user_id} -> "active" while the bypass lasts."""
    return f"{GROUP}:{chat_id}:vip:{user_id}"


# ---- Escalating strike system (24h TTL) ------------------------------------
STRIKE = f"{P}:strikes"  # + :{chat_id}:{user_id} -> int counter, TTL 86400s


def k_strike(chat_id: int, user_id: int) -> str:
    """abg:strikes:{chat_id}:{user_id} -> escalating strike counter (TTL 24h)."""
    return f"{STRIKE}:{chat_id}:{user_id}"


# ---- Hierarchical RBAC role storage ----------------------------------------
ROLE = f"{P}:role"  # + :{chat_id}:{user_id} -> role string
TITLE_PROTECTED = f"{P}:title:protected"  # + :{chat_id}:{user_id} -> "1"


def k_role(chat_id: int, user_id: int) -> str:
    """abg:role:{chat_id}:{user_id} -> hierarchical role string."""
    return f"{ROLE}:{chat_id}:{user_id}"


def k_title_protected(chat_id: int, user_id: int) -> str:
    """abg:title:protected:{chat_id}:{user_id} -> manual-title lock marker."""
    return f"{TITLE_PROTECTED}:{chat_id}:{user_id}"


# ---- SaaS subscription mirror + distributed heartbeat -----------------------
SUB = f"{P}:sub"                             # + :{chat_id} -> hash(status, expires)
HEARTBEAT_MASTER = f"{P}:heartbeat:master"   # TTL'd key, refreshed by the master
PRESENCE = f"{P}:presence"                   # hash: chat_id -> 1 (bots inside)


def k_sub_state(chat_id: int) -> str:
    """abg:sub:{chat_id} -> hash mirror of the GroupSubscription row."""
    return f"{SUB}:{chat_id}"


async def heartbeat_master() -> None:
    """Refresh the master liveness key (TTL 45s, refreshed every 15s)."""
    await get_redis().set(HEARTBEAT_MASTER, "1", ex=settings.heartbeat_ttl)


async def is_master_alive() -> bool:
    try:
        return bool(await get_redis().exists(HEARTBEAT_MASTER))
    except Exception:
        return True  # Redis unreachable: never take over blindly


async def presence_add(chat_id: int) -> None:
    try:
        await get_redis().hset(PRESENCE, str(chat_id), "1")
    except Exception:
        pass


async def presence_remove(chat_id: int) -> None:
    try:
        await get_redis().hdel(PRESENCE, str(chat_id))
    except Exception:
        pass


async def presence_known() -> list[int]:
    try:
        return [int(cid) for cid in await get_redis().hkeys(PRESENCE)]
    except Exception:
        return []


_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(
            settings.redis_dsn,
            encoding="utf-8",
            decode_responses=True,
            max_connections=64,
        )
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# --------------------------------------------------------------------------
# Temporary VIP / whitelist status - a 3-hour bypass of the media locks.
# --------------------------------------------------------------------------
VIP_TTL = 3 * 3600


async def set_user_vip(chat_id: int, user_id: int, duration_seconds: int = VIP_TTL) -> None:
    await get_redis().set(k_user_vip(chat_id, user_id), "active", ex=duration_seconds)


async def is_user_vip(chat_id: int, user_id: int) -> bool:
    return bool(await get_redis().get(k_user_vip(chat_id, user_id)))


async def revoke_user_vip(chat_id: int, user_id: int) -> None:
    await get_redis().delete(k_user_vip(chat_id, user_id))
