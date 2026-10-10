from __future__ import annotations

import logging

from abgguard.models.entities import Group, RoleBinding

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


async def known_titles(chat_ids: list[int]) -> dict[int, str]:
    if not chat_ids:
        return {}
    rows = await Group.filter(chat_id__in=chat_ids).values_list("chat_id", "title")
    return {int(c): t for c, t in rows if t}


async def managed_chat_ids() -> set[int]:
    """Every chat ABG Guard administers (group registry + RBAC bindings)."""
    groups = {int(c) for c in await Group.all().values_list("chat_id", flat=True)}
    bindings = {
        int(c) for c in await RoleBinding.all().values_list("chat_id", flat=True)
    }
    return groups | bindings
