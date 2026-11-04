from __future__ import annotations

import enum
import logging

from abgguard.core import redis as rk
from abgguard.core.redis import k_role

log = logging.getLogger("abgguard.hierarchy")

# --------------------------------------------------------------------------
# Role hierarchy (higher number = more authority)
# --------------------------------------------------------------------------
class Role(enum.StrEnum):
    GOD = "god"
    DEPUTY = "deputy"
    SENIOR_CHAT = "senior_chat"
    SENIOR_CALL = "senior_call"
    ADMIN_CHAT = "admin_chat"
    ADMIN_CALL = "admin_call"
    USER = "user"


# Authority level for permission checks
_LEVEL: dict[str, int] = {
    Role.GOD: 6,
    Role.DEPUTY: 5,
    Role.SENIOR_CHAT: 4,
    Role.SENIOR_CALL: 4,
    Role.ADMIN_CHAT: 3,
    Role.ADMIN_CALL: 3,
    Role.USER: 0,
}

# Who can appoint each role (set of appointing roles). GOD is intentionally
# restricted to GOD only; USER is the demotion target and is additionally
# guarded by can_demote() so nobody can remove an equal/higher-ranked member.
_CAN_APPOINT: dict[str, set[str]] = {
    Role.GOD: {Role.GOD},
    Role.DEPUTY: {Role.GOD},
    Role.SENIOR_CHAT: {Role.GOD, Role.DEPUTY},
    Role.SENIOR_CALL: {Role.GOD, Role.DEPUTY},
    Role.ADMIN_CHAT: {Role.GOD, Role.DEPUTY, Role.SENIOR_CHAT},
    Role.ADMIN_CALL: {Role.GOD, Role.DEPUTY, Role.SENIOR_CALL},
    Role.USER: {Role.GOD, Role.DEPUTY, Role.SENIOR_CHAT, Role.SENIOR_CALL},
}

# Uniform custom title templates (no emojis, <= 16 chars per Telegram API)
CUSTOM_TITLES: dict[str, str] = {
    Role.DEPUTY: "معاون مالک",
    Role.SENIOR_CHAT: "ارشد چت",
    Role.SENIOR_CALL: "ارشد کال",
    Role.ADMIN_CHAT: "ادمین چت",
    Role.ADMIN_CALL: "ادمین کال",
}

# Telegram permissions granted per role
ROLE_PERMISSIONS: dict[str, dict] = {
    Role.DEPUTY: {
        "can_change_info": True,
        "can_delete_messages": True,
        "can_invite_users": True,
        "can_restrict_members": True,
        "can_pin_messages": True,
        "can_promote_members": True,
        "can_manage_video_chats": True,
        "can_manage_voice_chats": True,
    },
    Role.SENIOR_CHAT: {
        "can_delete_messages": True,
        "can_invite_users": True,
        "can_restrict_members": True,
        "can_pin_messages": True,
        "can_promote_members": False,
        "can_manage_video_chats": False,
        "can_manage_voice_chats": False,
    },
    Role.SENIOR_CALL: {
        "can_delete_messages": True,
        "can_invite_users": True,
        "can_restrict_members": False,
        "can_pin_messages": False,
        "can_promote_members": False,
        "can_manage_video_chats": True,
        "can_manage_voice_chats": True,
    },
    Role.ADMIN_CHAT: {
        "can_delete_messages": True,
        "can_invite_users": True,
        "can_restrict_members": False,
        "can_pin_messages": False,
        "can_promote_members": False,
        "can_manage_video_chats": False,
        "can_manage_voice_chats": False,
    },
    Role.ADMIN_CALL: {
        "can_delete_messages": False,
        "can_invite_users": False,
        "can_restrict_members": False,
        "can_pin_messages": False,
        "can_promote_members": False,
        "can_manage_video_chats": True,
        "can_manage_voice_chats": True,
    },
}

# Persian labels for each role
ROLE_LABELS_FA: dict[str, str] = {
    Role.GOD: "گاد ادمین",
    Role.DEPUTY: "معاون",
    Role.SENIOR_CHAT: "ارشد چت",
    Role.SENIOR_CALL: "ارشد کال",
    Role.ADMIN_CHAT: "ادمین چت",
    Role.ADMIN_CALL: "ادمین کال",
    Role.USER: "کاربر",
}

# Command aliases for promotion/demotion
PROMOTE_ALIASES: dict[str, str] = {
    "نصب معاون": Role.DEPUTY,
    "عزل معاون": Role.USER,
    "نصب ارشد چت": Role.SENIOR_CHAT,
    "عزل ارشد چت": Role.USER,
    "نصب ارشد کال": Role.SENIOR_CALL,
    "عزل ارشد کال": Role.USER,
    "نصب ادمین چت": Role.ADMIN_CHAT,
    "عزل ادمین چت": Role.USER,
    "نصب ادمین کال": Role.ADMIN_CALL,
    "عزل ادمین کال": Role.USER,
}


def level(role: str) -> int:
    """Return the authority level of a role."""
    return _LEVEL.get(role, 0)


def can_demote(actor_role: str, current_role: str) -> bool:
    """Return True when actor outranks the member they are demoting.

    The current rank decides the permission, not the destination: this stops a
    Senior Chat from stripping a Deputy simply because both end at USER.
    """
    if current_role in {Role.USER, Role.GOD}:
        return False
    if actor_role == Role.GOD:
        return True
    if level(actor_role) <= level(current_role):
        return False
    return actor_role in _CAN_APPOINT.get(current_role, set())


def can_appoint(
    actor_role: str, target_role: str, current_role: str | None = None
) -> bool:
    """Return True if actor_role can appoint/demote target_role.

    ``current_role`` is required for demotions (``target_role == USER``) so the
    check can compare the actor against the member's existing rank.
    """
    if target_role == Role.USER:
        if current_role is not None:
            return can_demote(actor_role, current_role)
        return actor_role in _CAN_APPOINT.get(Role.USER, set())
    return actor_role in _CAN_APPOINT.get(target_role, set())


def custom_title(role: str) -> str:
    """Return the uniform custom title for a role."""
    return CUSTOM_TITLES.get(role, "")


def permissions_for(role: str) -> dict:
    """Return the Telegram permissions dict for a role."""
    return ROLE_PERMISSIONS.get(role, {})


def label_fa(role: str) -> str:
    """Return the Persian label for a role."""
    return ROLE_LABELS_FA.get(role, "کاربر")


# --------------------------------------------------------------------------
# Redis-backed role operations
# --------------------------------------------------------------------------
async def get_role(chat_id: int, user_id: int) -> str:
    """Get the hierarchical role of a user in a chat."""
    r = rk.get_redis()
    raw = await r.get(k_role(chat_id, user_id))
    if raw is None:
        return Role.USER
    return raw.decode() if isinstance(raw, bytes) else raw


async def set_role(
    chat_id: int, user_id: int, role: str, ttl: int | None = None
) -> None:
    """Set the hierarchical role of a user in a chat."""
    r = rk.get_redis()
    await r.set(k_role(chat_id, user_id), role, ex=ttl)
    log.info("ROLE set: chat=%s user=%s role=%s", chat_id, user_id, role)


async def remove_role(chat_id: int, user_id: int) -> None:
    """Remove the hierarchical role of a user in a chat (revert to USER)."""
    r = rk.get_redis()
    await r.delete(k_role(chat_id, user_id))
    log.info("ROLE removed: chat=%s user=%s", chat_id, user_id)
