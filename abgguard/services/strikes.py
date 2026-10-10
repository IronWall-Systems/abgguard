from __future__ import annotations

from abgguard.core import redis as rk

OFFENSE_TTL = 7 * 24 * 3600


def k_offense(chat_id: int, user_id: int) -> str:
    return f"{rk.P}:offense:{chat_id}:{user_id}"


async def register(chat_id: int, user_id: int) -> int:
    """
    Count one more strike for (chat_id, user_id) and return the new total.

    Shared by the automatic guard ladder and the manual moderation commands so
    an "اخطار" issued by a moderator and a strike earned by a flood behave as
    one single counter (first strike -> timed mute, hitting the ceiling ->
    permanent mute plus a private escalation to the Level 2 owner).

    The counter never bans on its own: removing a member stays a human
    decision (Level 2/3 replying «بن»), see ``services/moderation.py``.
    """
    r = rk.get_redis()
    key = k_offense(chat_id, user_id)
    n = await r.incr(key)
    if int(n) == 1:
        await r.expire(key, OFFENSE_TTL)
    return int(n)


async def current(chat_id: int, user_id: int) -> int:
    raw = await rk.get_redis().get(k_offense(chat_id, user_id))
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


async def reset(chat_id: int, user_id: int) -> None:
    await rk.get_redis().delete(k_offense(chat_id, user_id))
