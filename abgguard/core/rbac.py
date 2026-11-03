from __future__ import annotations

import enum
import logging

from abgguard.config import FALLBACK_GOD_ADMIN_ID, settings
from abgguard.core import redis as rk
from abgguard.models.entities import GodAdmin, RoleBinding

log = logging.getLogger("abgguard.rbac")

ROLE_TTL = 30  # seconds, hot cache in front of Redis sets


def configured_gods() -> set[int]:
    """Env-declared Level 3 ids, plus the built-in fallback id.

    These are authoritative: neither an empty env nor a failed PostgreSQL
    write is allowed to revoke them.
    """
    return set(settings.god_admin_ids) or {FALLBACK_GOD_ADMIN_ID}


class Role(enum.IntEnum):
    USER = 0
    SUBADMIN = 1
    ADMIN = 2
    GOD = 3

    @property
    def label_fa(self) -> str:
        return {
            Role.USER: "کاربر",
            Role.SUBADMIN: "ساب‌ادمین",
            Role.ADMIN: "ادمین گروه",
            Role.GOD: "گاد ادمین",
        }[self]


# --------------------------------------------------------------------------
# bootstrap / hydrate
# --------------------------------------------------------------------------
async def bootstrap() -> None:
    """Seed env-declared God Admins, then hydrate Redis from PostgreSQL."""
    r = rk.get_redis()
    for uid in configured_gods():
        await r.sadd(rk.RBAC_GOD, str(uid))
        await GodAdmin.get_or_create(user_id=uid, defaults={"note": "env seed"})

    await hydrate()


async def hydrate() -> None:
    """Rebuild the Redis RBAC sets from the source of truth (PostgreSQL)."""
    r = rk.get_redis()

    gods = set(await GodAdmin.all().values_list("user_id", flat=True)) | configured_gods()
    admins = await RoleBinding.filter(role=RoleBinding.ADMIN).all().values_list(
        "chat_id", "user_id"
    )
    subs = await RoleBinding.filter(role=RoleBinding.SUBADMIN).all().values_list(
        "chat_id", "user_id"
    )

    pipe = r.pipeline(transaction=False)
    pipe.delete(rk.RBAC_GOD)
    for uid in gods:
        pipe.sadd(rk.RBAC_GOD, str(uid))
    for chat_id, user_id in admins:
        pipe.sadd(rk.k_admin(chat_id), str(user_id))
    for chat_id, user_id in subs:
        pipe.sadd(rk.k_sub(chat_id), str(user_id))
    await pipe.execute()

    log.info(
        "RBAC hydrated: %d god, %d admin bindings, %d sub-admin bindings",
        len(gods), len(admins), len(subs),
    )


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------
async def is_god(user_id: int) -> bool:
    return bool(await rk.get_redis().sismember(rk.RBAC_GOD, str(user_id)))


async def get_role(chat_id: int | None, user_id: int) -> Role:
    r = rk.get_redis()

    if await is_god(user_id):
        return Role.GOD
    if chat_id is None:
        return Role.USER

    cache_key = rk.k_rbac_cache(chat_id, user_id)
    cached = await r.get(cache_key)
    if cached is not None:
        try:
            return Role(int(cached))
        except ValueError:
            pass

    if await r.sismember(rk.k_admin(chat_id), str(user_id)):
        role = Role.ADMIN
    elif await r.sismember(rk.k_sub(chat_id), str(user_id)):
        role = Role.SUBADMIN
    else:
        role = Role.USER

    await r.set(cache_key, str(int(role)), ex=ROLE_TTL)
    return role


async def can_open_panel(chat_id: int, user_id: int) -> bool:
    return await get_role(chat_id, user_id) >= Role.SUBADMIN


async def can_manage_subadmins(chat_id: int, user_id: int) -> bool:
    """Level 2 (Admin) and Level 3 (God) only - Level 1 is excluded."""
    return await get_role(chat_id, user_id) >= Role.ADMIN


async def can_grant_admin(user_id: int) -> bool:
    return await is_god(user_id)


async def list_admins(chat_id: int) -> list[int]:
    members = await rk.get_redis().smembers(rk.k_admin(chat_id))
    return sorted(int(m) for m in members)


async def list_subadmins(chat_id: int) -> list[int]:
    members = await rk.get_redis().smembers(rk.k_sub(chat_id))
    return sorted(int(m) for m in members)


# --------------------------------------------------------------------------
# private-chat (PV) resolution - the PV chat_id is *not* a group, so the
# group-scoped Redis sets above cannot be consulted. PostgreSQL (the durable
# source of truth) is queried instead - never cached indefinitely.
# --------------------------------------------------------------------------
async def get_pv_role(user_id: int) -> Role:
    """Highest role this user holds anywhere (used to route the PV dashboard)."""
    if await is_god(user_id):
        return Role.GOD
    roles = await RoleBinding.filter(user_id=user_id).values_list("role", flat=True)
    if RoleBinding.ADMIN in roles:
        return Role.ADMIN
    if RoleBinding.SUBADMIN in roles:
        return Role.SUBADMIN
    return Role.USER


async def list_managed_chats(user_id: int) -> list[tuple[int, int]]:
    """``[(chat_id, role), ...]`` - every group this user may configure."""
    rows = await RoleBinding.filter(user_id=user_id).values_list("chat_id", "role")
    best: dict[int, int] = {}
    for chat_id, role in rows:
        best[int(chat_id)] = max(best.get(int(chat_id), 0), int(role))
    return sorted(best.items())


async def role_in(chat_id: int, user_id: int) -> Role:
    """Re-read the binding from PostgreSQL - used on privileged PV actions."""
    if await is_god(user_id):
        return Role.GOD
    rows = await RoleBinding.filter(chat_id=chat_id, user_id=user_id).values_list(
        "role", flat=True
    )
    if RoleBinding.ADMIN in rows:
        return Role.ADMIN
    if RoleBinding.SUBADMIN in rows:
        return Role.SUBADMIN
    return Role.USER


async def list_owner_bindings() -> list[tuple[int, int]]:
    """``[(chat_id, user_id), ...]`` - every Level 2 assignment (Level 3 view)."""
    rows = await RoleBinding.filter(role=RoleBinding.ADMIN).values_list(
        "chat_id", "user_id"
    )
    return sorted((int(c), int(u)) for c, u in rows)


async def list_owner_ids(chat_id: int) -> list[int]:
    """Durable Level 2 roster for one chat (PostgreSQL, not the Redis mirror)."""
    rows = await RoleBinding.filter(
        chat_id=chat_id, role=RoleBinding.ADMIN
    ).values_list("user_id", flat=True)
    return sorted(int(u) for u in rows)


async def list_gods() -> list[int]:
    return sorted(int(m) for m in await rk.get_redis().smembers(rk.RBAC_GOD))


# --------------------------------------------------------------------------
# writes  (PostgreSQL first -> Redis mirror -> cache invalidation)
# --------------------------------------------------------------------------
async def _invalidate(chat_id: int, user_id: int) -> None:
    await rk.get_redis().delete(rk.k_rbac_cache(chat_id, user_id))


async def add_god(user_id: int, granted_by: int | None = None, note: str | None = None) -> None:
    await GodAdmin.get_or_create(
        user_id=user_id, defaults={"note": note or f"granted by {granted_by}"}
    )
    await rk.get_redis().sadd(rk.RBAC_GOD, str(user_id))
    log.info("GOD admin added: %s", user_id)


async def remove_god(user_id: int) -> None:
    await GodAdmin.filter(user_id=user_id).delete()
    await rk.get_redis().srem(rk.RBAC_GOD, str(user_id))


async def add_admin(chat_id: int, user_id: int, granted_by: int) -> None:
    """Level 2 grant - ONLY a God Admin may call this."""
    if not await is_god(granted_by):
        raise PermissionError("only GOD can grant ADMIN role")
    if await is_god(user_id):
        raise ValueError("user is already GOD")

    await RoleBinding.update_or_create(
        chat_id=chat_id,
        user_id=user_id,
        defaults={"role": RoleBinding.ADMIN, "granted_by": granted_by},
    )
    # an admin can never downgrade himself / never be a sub-admin too
    await rk.get_redis().srem(rk.k_sub(chat_id), str(user_id))
    await rk.get_redis().sadd(rk.k_admin(chat_id), str(user_id))
    await _invalidate(chat_id, user_id)
    log.info("ADMIN granted: user=%s chat=%s by=%s", user_id, chat_id, granted_by)


async def remove_admin(chat_id: int, user_id: int, requested_by: int) -> None:
    if not await is_god(requested_by) and await get_role(chat_id, requested_by) < Role.ADMIN:
        raise PermissionError("not allowed")
    await RoleBinding.filter(
        chat_id=chat_id, user_id=user_id, role=RoleBinding.ADMIN
    ).delete()
    await rk.get_redis().srem(rk.k_admin(chat_id), str(user_id))
    await _invalidate(chat_id, user_id)


async def add_subadmin(chat_id: int, user_id: int, granted_by: int) -> None:
    """Level 1 grant - Level 2 (or Level 3) only. Never inside another chat."""
    if not await can_manage_subadmins(chat_id, granted_by):
        raise PermissionError("only ADMIN/GOD can grant SUBADMIN role")
    if user_id == granted_by:
        raise ValueError("cannot demote yourself to sub-admin")
    if await is_god(user_id):
        raise ValueError("GOD admin cannot be a sub-admin")
    if await get_role(chat_id, user_id) >= Role.ADMIN:
        raise ValueError("user already has a higher role in this chat")

    await RoleBinding.update_or_create(
        chat_id=chat_id,
        user_id=user_id,
        defaults={"role": RoleBinding.SUBADMIN, "granted_by": granted_by},
    )
    await rk.get_redis().sadd(rk.k_sub(chat_id), str(user_id))
    await _invalidate(chat_id, user_id)
    log.info("SUBADMIN granted: user=%s chat=%s by=%s", user_id, chat_id, granted_by)


async def remove_subadmin(chat_id: int, user_id: int, requested_by: int) -> None:
    if not await can_manage_subadmins(chat_id, requested_by):
        raise PermissionError("not allowed")
    await RoleBinding.filter(
        chat_id=chat_id, user_id=user_id, role=RoleBinding.SUBADMIN
    ).delete()
    await rk.get_redis().srem(rk.k_sub(chat_id), str(user_id))
    await _invalidate(chat_id, user_id)
