from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from aiogram import Bot
from aiogram.types import Chat, Message, Update, User

from abgguard.core import rbac
from abgguard.core.rbac import Role
from abgguard.master.handlers import modcmd
from abgguard.services import moderation, strikes

_UPDATE_ID = 1000
BOT_USER = User(id=1000, is_bot=True, first_name="ABG Guard", username="abg_guard_bot")


class Recorder:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.role: Role = Role.USER
        self.roles: dict[int, Role] = {}
        self.deleted: list[tuple[int, int]] = []
        self.mutes: list[tuple[int, int, int]] = []
        self.permamutes: list[tuple[int, int]] = []
        self.bans: list[tuple[int, int]] = []
        self.logs: list[str] = []
        self.notices: list[str] = []
        self.bulk: list[tuple[int, list[int]]] = []
        self.modlogs: list[dict] = []
        self.escalations: list[dict] = []
        self.restricts: list[tuple[int, int, object]] = []
        self.delays: list[float] = []
        self.strike = 1

    def staff(self, role: Role, *uids: int) -> None:
        """The actor (uid 7) gets ``role``; everybody else stays a plain member."""
        self.roles = dict.fromkeys((7, *uids), role)
        self.role = Role.USER


REC = Recorder()


def _uid() -> int:
    global _UPDATE_ID
    _UPDATE_ID += 1
    return _UPDATE_ID


def _message(
    text: str,
    *,
    chat_type: str = "supergroup",
    chat_id: int = -1001,
    uid: int = 7,
    message_id: int = 5,
    reply: Message | None = None,
) -> Message:
    chat = (
        Chat(id=chat_id, type="private")
        if chat_type == "private"
        else Chat(id=chat_id, type=chat_type, title="گروه تست")
    )
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=chat,
        from_user=User(id=uid, is_bot=False, first_name="علی"),
        text=text,
        reply_to_message=reply,
    )


# ------------------------------------------------------------------ hooks ----
async def _safe_delete(bot, chat_id, message_id):
    REC.deleted.append((chat_id, message_id))
    return True


async def _mute(bot, chat_id, user_id, seconds):
    REC.mutes.append((chat_id, user_id, seconds))
    return True


async def _perm_mute(bot, chat_id, user_id):
    REC.permamutes.append((chat_id, user_id))
    return True


async def _ban(bot, chat_id, user_id):
    REC.bans.append((chat_id, user_id))
    return True


async def _notice(message, text, *, delay=10.0):
    REC.notices.append(text)
    REC.delays.append(delay)


async def _get_role(chat_id, user_id):
    return REC.roles.get(user_id, REC.role)


def _modlog_notify(bot, chat_id, **kwargs):
    REC.modlogs.append({"chat_id": chat_id, **kwargs})


def _modlog_escalate(bot, chat_id, **kwargs):
    REC.escalations.append({"chat_id": chat_id, **kwargs})


async def _restrict(self, chat_id, user_id=None, permissions=None, **kwargs):
    REC.restricts.append((chat_id, user_id, permissions))
    return True


async def _register_strike(chat_id, user_id):
    return REC.strike


async def _bot_me(self):
    return BOT_USER


async def _delete_messages(self, chat_id, message_ids, **kwargs):
    REC.bulk.append((chat_id, list(message_ids)))
    return True


class _StubLog:
    @classmethod
    async def create(cls, **kwargs) -> None:
        REC.logs.append(str(kwargs["reason"]))


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    REC.reset()
    monkeypatch.setattr(moderation, "safe_delete", _safe_delete)
    monkeypatch.setattr(moderation, "mute", _mute)
    monkeypatch.setattr(moderation, "perm_mute", _perm_mute)
    monkeypatch.setattr(moderation, "ban", _ban)
    monkeypatch.setattr(modcmd, "notice", _notice)
    monkeypatch.setattr(modcmd, "BanLog", _StubLog)
    monkeypatch.setattr(modcmd.modlog, "notify", _modlog_notify)
    monkeypatch.setattr(modcmd.modlog, "escalate", _modlog_escalate)
    monkeypatch.setattr(rbac, "get_role", _get_role)
    monkeypatch.setattr(strikes, "register", _register_strike)
    monkeypatch.setattr(Bot, "me", _bot_me)
    monkeypatch.setattr(Bot, "delete_messages", _delete_messages)
    monkeypatch.setattr(Bot, "restrict_chat_member", _restrict)


def _feed(master, message: Message) -> None:
    bot, dp = master
    update = Update(update_id=_uid(), message=message)
    asyncio.run(dp.feed_update(bot=bot, update=update))


def _staff_reply(text: str, **kwargs) -> Message:
    target = _message("پیام متخلف", uid=99, message_id=4)
    return _message(text, reply=target, **kwargs)


# ------------------------------------------------------------------ filter ---
def test_a_bare_persian_trigger_is_only_a_command_for_the_staff() -> None:
    filt = modcmd.ModTrigger()

    REC.role = Role.SUBADMIN
    assert asyncio.run(filt(message=_message("سکوت"))) is True

    REC.role = Role.USER
    assert asyncio.run(filt(message=_message("سکوت"))) is False
    assert asyncio.run(filt(message=_message("سکوت کن"))) is False


def test_slash_commands_always_reach_the_handler() -> None:
    filt = modcmd.ModTrigger()
    REC.role = Role.USER
    assert asyncio.run(filt(message=_message("/mute"))) is True
    assert asyncio.run(filt(message=_message("/unmute"))) is True
    assert asyncio.run(filt(message=_message("/پاکسازی"))) is True
    assert asyncio.run(filt(message=_message("/panel"))) is False
    assert asyncio.run(filt(message=_message("hello"))) is False


def test_the_unmute_bare_triggers_are_staff_only() -> None:
    filt = modcmd.ModTrigger()

    REC.staff(Role.SUBADMIN)
    assert asyncio.run(filt(message=_message("لغو سکوت"))) is True
    assert asyncio.run(filt(message=_message("حذف سکوت"))) is True

    REC.staff(Role.USER)
    assert asyncio.run(filt(message=_message("لغو سکوت"))) is False
    assert asyncio.run(filt(message=_message("حذف سکوت"))) is False


# ------------------------------------------------------------ silent remove ---
def test_the_remove_trigger_needs_a_reply_and_the_staff_rank() -> None:
    filt = modcmd.RemoveTrigger()

    REC.staff(Role.SUBADMIN)
    assert asyncio.run(filt(message=_staff_reply("remove"))) is True
    assert asyncio.run(filt(message=_staff_reply("حذف"))) is True
    assert asyncio.run(filt(message=_staff_reply("  حذف  "))) is True

    REC.staff(Role.USER)
    assert asyncio.run(filt(message=_staff_reply("حذف"))) is False

    REC.staff(Role.ADMIN)
    assert asyncio.run(filt(message=_message("حذف"))) is False, "no reply, no command"
    assert asyncio.run(filt(message=_staff_reply("حذف شود"))) is False
    assert asyncio.run(filt(message=_staff_reply("remove it"))) is False
    assert asyncio.run(filt(message=_staff_reply("/حذف"))) is False


def test_remove_deletes_the_message_then_the_trigger_and_stays_silent(master) -> None:
    REC.staff(Role.SUBADMIN)
    _feed(master, _staff_reply("remove"))

    assert REC.deleted == [(-1001, 4), (-1001, 5)]  # the message, then the trigger
    assert REC.notices == [], "rule 6: a silent tool sends no confirmation"
    assert REC.mutes == [] and REC.permamutes == [] and REC.bans == []
    assert REC.logs == [] and REC.modlogs == [] and REC.escalations == []


def test_remove_also_answers_to_the_persian_word(master) -> None:
    REC.staff(Role.GOD)
    _feed(master, _staff_reply("حذف", message_id=6))

    assert REC.deleted == [(-1001, 4), (-1001, 6)]
    assert REC.notices == []
    assert REC.mutes == [] and REC.bans == [] and REC.logs == []


def test_remove_never_deletes_a_bot_authored_message(master) -> None:
    """Rule §9: the panel / notices of the bots are never touched."""
    REC.staff(Role.ADMIN)
    panel = Message(
        message_id=11,
        date=datetime.now(UTC),
        chat=Chat(id=-1001, type="supergroup", title="گروه تست"),
        from_user=User(id=BOT_USER.id, is_bot=True, first_name=BOT_USER.first_name),
        text="پنل",
    )
    _feed(master, _message("حذف", reply=panel, message_id=10))

    assert REC.deleted == [(-1001, 10)], "only the trigger itself disappears"



# ----------------------------------------------------------------- actions ---
def test_mute_from_a_reply(master) -> None:
    REC.staff(Role.SUBADMIN)
    _feed(master, _staff_reply("/mute"))

    assert REC.mutes == [(-1001, 99, modcmd.DEFAULT_MUTE)]
    assert REC.deleted == [(-1001, 5), (-1001, 4)]  # trigger first, then the message
    assert REC.logs == [f"mute:سکوت دستی:{modcmd.DEFAULT_MUTE}s"]
    assert "سکوت شد" in REC.notices[0]
    assert REC.modlogs == [{
        "chat_id": -1001,
        "action": f"سکوت {modcmd.humanize(modcmd.DEFAULT_MUTE)}",
        "target": 99,
        "actor": 7,
        "actor_source": "admin",
        "reason": "سکوت دستی",
    }]


def test_mute_honours_the_requested_duration(master) -> None:
    REC.staff(Role.SUBADMIN)
    _feed(master, _staff_reply("/mute 45s"))
    assert REC.mutes == [(-1001, 99, 45)]


def test_ban_from_a_reply(master) -> None:
    REC.staff(Role.ADMIN)
    _feed(master, _staff_reply("/بن"))

    assert REC.bans == [(-1001, 99)]
    assert REC.logs == ["بن دستی"]
    assert "بن شد" in REC.notices[0]
    assert REC.modlogs[-1]["action"] == "بن"
    assert REC.modlogs[-1]["actor"] == 7
    assert REC.modlogs[-1]["actor_source"] == "admin"


def test_a_sub_admin_cannot_ban(master) -> None:
    """Level 1 may silence and warn, but never remove a member (rule §3)."""
    REC.staff(Role.SUBADMIN)
    _feed(master, _staff_reply("/بن"))

    assert REC.bans == []
    assert REC.logs == []
    assert REC.notices == [modcmd.BAN_NOTICE]
    assert REC.deleted == [(-1001, 5)]  # the trigger still disappears


def test_a_sub_admin_cannot_ban_through_the_kick_alias(master) -> None:
    REC.staff(Role.SUBADMIN)
    _feed(master, _staff_reply("/kick"))

    assert REC.bans == []
    assert REC.notices == [modcmd.BAN_NOTICE]


def test_god_admin_keeps_the_ban(master) -> None:
    REC.staff(Role.GOD)
    _feed(master, _staff_reply("/ban"))

    assert REC.bans == [(-1001, 99)]
    assert REC.modlogs[-1]["action"] == "بن"


def test_a_sub_admin_can_full_mute(master) -> None:
    REC.staff(Role.SUBADMIN)
    _feed(master, _staff_reply("/full_mute"))

    assert REC.permamutes == [(-1001, 99)]
    assert REC.bans == [] and REC.mutes == []
    assert REC.logs == ["سکوت دائم"]
    assert "همیشه سکوت شد" in REC.notices[0]
    assert REC.modlogs[-1]["action"] == "سکوت دائم"
    assert REC.modlogs[-1]["target"] == 99


def test_unmute_restores_the_default_send_rights(master) -> None:
    REC.staff(Role.SUBADMIN)
    _feed(master, _staff_reply("/unmute"))

    assert len(REC.restricts) == 1
    chat_id, user_id, perms = REC.restricts[0]
    assert (chat_id, user_id) == (-1001, 99)
    assert perms.can_send_messages is True
    assert perms.can_send_documents is True
    assert perms.can_send_photos is True
    assert perms.can_send_other_messages is True
    assert perms.can_add_web_page_previews is True
    assert REC.deleted == [(-1001, 5)]  # clean chat: the command itself goes first
    assert REC.notices[0].startswith("🔊")
    assert "لغو شد" in REC.notices[0]
    assert REC.delays[0] == 7.0
    assert REC.bans == [] and REC.mutes == [] and REC.permamutes == []


def test_unmute_also_answers_to_its_bare_persian_triggers(master) -> None:
    REC.staff(Role.ADMIN)
    _feed(master, _staff_reply("لغو سکوت"))
    assert [(c, u) for c, u, _ in REC.restricts] == [(-1001, 99)]

    REC.staff(Role.SUBADMIN)
    _feed(master, _staff_reply("حذف سکوت", message_id=6))
    assert [(c, u) for c, u, _ in REC.restricts] == [(-1001, 99), (-1001, 99)]
    assert "لغو شد" in REC.notices[-1]


def test_a_member_cannot_unmute_anybody(master) -> None:
    REC.staff(Role.USER)
    _feed(master, _staff_reply("/unmute"))

    assert REC.restricts == []
    assert "تیم مدیریت" in REC.notices[0]


def test_warn_mutes_on_the_first_strike_and_mutes_forever_on_the_second(master) -> None:
    REC.staff(Role.SUBADMIN)

    REC.strike = 1
    _feed(master, _staff_reply("/اخطار"))
    assert REC.mutes == [(-1001, 99, 600)]
    assert REC.bans == []
    assert "اخطار ۱" in REC.notices[0]
    assert REC.escalations == []

    assert REC.modlogs[-1]["action"].startswith("اخطار 1")

    REC.strike = 2
    _feed(master, _staff_reply("/warn"))
    assert REC.bans == [], "the ladder never bans on its own"
    assert REC.permamutes == [(-1001, 99)]
    assert "سکوت شد" in REC.notices[-1]
    assert REC.modlogs[-1]["action"] == "اخطار 2 — سکوت دائم"
    assert REC.escalations == [{
        "chat_id": -1001,
        "target": 99,
        "reason": "اخطار دستی (2)",
        "target_name": "علی",
    }]


def test_purge_bulk_deletes_the_requested_window(master) -> None:
    REC.staff(Role.SUBADMIN)
    _feed(master, _message("/purge 30", message_id=100))

    assert REC.bulk == [(-1001, list(range(99, 69, -1)))]
    assert "۳۰" in REC.notices[0]


def test_purge_defaults_to_twenty_messages(master) -> None:
    REC.staff(Role.SUBADMIN)
    _feed(master, _message("/پاکسازی", message_id=100))
    assert len(REC.bulk[0][1]) == modcmd.DEFAULT_PURGE


def test_a_member_gets_a_short_refusal_and_no_action(master) -> None:
    REC.staff(Role.USER)
    _feed(master, _staff_reply("/mute"))

    assert REC.mutes == [] and REC.bans == []
    assert "تیم مدیریت" in REC.notices[0]
    assert REC.deleted == [(-1001, 5)]  # the trigger still disappears


def test_missing_target_is_reported(master) -> None:
    REC.staff(Role.SUBADMIN)
    _feed(master, _message("/mute"))

    assert REC.mutes == []
    assert "ریپلای" in REC.notices[0]


def test_self_punishment_is_refused(master) -> None:
    REC.staff(Role.ADMIN)
    own_message = _message("پیام خودم", uid=7, message_id=4)
    _feed(master, _message("/ban", uid=7, reply=own_message))

    assert REC.bans == []
    assert "خودتان" in REC.notices[0]


def test_a_higher_ranked_member_is_protected(master) -> None:
    REC.staff(Role.ADMIN)
    REC.roles[99] = Role.ADMIN
    _feed(master, _staff_reply("/ban"))

    assert REC.bans == []
    assert "هم‌سطح" in REC.notices[0]


def test_the_bot_itself_is_protected(master) -> None:
    REC.staff(Role.GOD)
    target = _message("x", uid=BOT_USER.id, message_id=4)
    _feed(master, _message("/ban", reply=target))

    assert REC.bans == []
    assert "ربات" in REC.notices[0]
