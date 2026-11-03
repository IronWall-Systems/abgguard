from __future__ import annotations

import logging

from abgguard.config import settings
from abgguard.core import redis as rk
from abgguard.models.entities import Group, GroupSubscription, RoleBinding

log = logging.getLogger("abgguard.groups")


async def upsert_group(
    chat_id: int, title: str | None = None, username: str | None = None
) -> None:
    """Remember a managed group (title is what the PV selector renders)."""
    try:
        row, created = await Group.get_or_create(
            chat_id=chat_id, defaults={"title": title, "username": username}
        )
        if not created and title and title != row.title:
            row.title = title
            await row.save(update_fields=["title"])
    except Exception:  # never let bookkeeping break a guard/panel path
        log.debug("group upsert failed chat=%s", chat_id, exc_info=True)


async def ensure_owner(chat_id: int, owner_id: int | None = None) -> None:
    """Bind an explicitly assigned Level 2 owner (never the God Admin).

    The God Admin already has global access through `rbac.is_god`, so binding
    them per-group would only hide the group's real creator/admin. The Redis
    admin set is updated in the same step so the PV panel and mod-log fan-out
    see the change immediately.
    """
    if owner_id is None:
        return
    god = int(settings.god_admin_id)
    user_id = int(owner_id)
    if user_id == god:
        return
    try:
        await RoleBinding.update_or_create(
            chat_id=chat_id,
            user_id=user_id,
            defaults={"role": RoleBinding.ADMIN, "granted_by": god},
        )
        await rk.get_redis().sadd(rk.k_admin(chat_id), str(user_id))
    except Exception:
        log.debug("owner sync failed chat=%s", chat_id, exc_info=True)


async def known_titles(chat_ids: list[int]) -> dict[int, str]:
    if not chat_ids:
        return {}
    rows = await Group.filter(chat_id__in=chat_ids).values_list("chat_id", "title")
    return {int(c): t for c, t in rows if t}


async def managed_chat_ids() -> set[int]:
    """Every tracked chat: group registry, RBAC bindings and subscriptions."""
    groups = {int(c) for c in await Group.all().values_list("chat_id", flat=True)}
    bindings = {
        int(c) for c in await RoleBinding.all().values_list("chat_id", flat=True)
    }
    subs = {
        int(c) for c in await GroupSubscription.all().values_list("chat_id", flat=True)
    }
    return groups | bindings | subs


async def remove_chat(chat_id: int) -> None:
    """Remove a group from every roster, RBAC binding and Redis cache."""
    try:
        user_ids = {
            int(u)
            for u in await RoleBinding.filter(chat_id=chat_id).values_list(
                "user_id", flat=True
            )
        }
    except Exception:
        user_ids = set()
    try:
        await Group.filter(chat_id=chat_id).delete()
    except Exception:
        log.debug("group row removal failed chat=%s", chat_id, exc_info=True)
    try:
        await GroupSubscription.filter(chat_id=chat_id).delete()
    except Exception:
        log.debug("subscription row removal failed chat=%s", chat_id, exc_info=True)
    try:
        await RoleBinding.filter(chat_id=chat_id).delete()
    except Exception:
        log.debug("role binding removal failed chat=%s", chat_id, exc_info=True)
    try:
        r = rk.get_redis()
        keys = [
            rk.k_admin(chat_id),
            rk.k_sub(chat_id),
            rk.k_sub_state(chat_id),
            *[rk.k_rbac_cache(chat_id, uid) for uid in user_ids],
        ]
        await r.delete(*keys)
    except Exception:
        log.debug("group cache removal failed chat=%s", chat_id, exc_info=True)
