from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.types import Chat, Message, PhotoSize, Update, User

from abgguard.core import groups as groups_store
from abgguard.core import rbac, settings_store
from abgguard.core.rbac import Role
from abgguard.core.settings_store import GroupFlags
from abgguard.services import textguards

ADMIN_ID = 6029973681
BOT_ID = 8837855460
CHAT_ID = -100111222333

_uid = [500000]
DELETES: list[tuple[str, int]] = []
SENT: list[Message] = []


def _next() -> int:
    _uid[0] += 1
    return _uid[0]


async def _get_role(chat_id: int, user_id: int) -> Role:
    return Role.ADMIN if user_id == ADMIN_ID else Role.USER


async def _get_flags(chat_id: int) -> GroupFlags:
    # every lock ON - if the bot whitelist ever regresses, these tests fail
    return GroupFlags(chat_id=chat_id, emoji_lock=True, link_lock=True, photo_lock=True)


async def _no_badword(chat_id: int, text: str):
    return None


async def _upsert(*args, **kwargs) -> None:
    return None


async def _msg_delete(self, *args, **kwargs):
    DELETES.append(("Message.delete", int(self.message_id)))
    return True


async def _bot_delete_message(self, chat_id, message_id, *args, **kwargs):
    DELETES.append(("Bot.delete_message", int(message_id)))
    return True


async def _bot_send_message(self, chat_id, text=None, *args, **kwargs):
    return _bot_message(Chat(id=chat_id, type="supergroup", title="g"), str(text))


async def _msg_answer(self, text=None, reply_markup=None, **kwargs):
    return _bot_message(self.chat, str(text), reply_markup=reply_markup)


def _bot_message(chat, text, reply_markup=None) -> Message:
    sent = Message(
        message_id=_next(),
        date=datetime.now(UTC),
        chat=chat,
        from_user=User(id=BOT_ID, is_bot=True, first_name="ABGGUARDBOT"),
        text=text,
        reply_markup=reply_markup,
    )
    SENT.append(sent)
    return sent


def _group_msg(text: str, *, user_id: int, is_bot: bool) -> Message:
    return Message(
        message_id=_next(),
        date=datetime.now(UTC),
        chat=Chat(id=CHAT_ID, type="supergroup", title="گروه تست"),
        from_user=User(id=user_id, is_bot=is_bot, first_name="کاربر"),
        text=text,
    )


def _photo_msg(*, user_id: int, is_bot: bool) -> Message:
    return Message(
        message_id=_next(),
        date=datetime.now(UTC),
        chat=Chat(id=CHAT_ID, type="supergroup", title="گروه تست"),
        from_user=User(id=user_id, is_bot=is_bot, first_name="کاربر"),
        photo=[
            PhotoSize(file_id="f", file_unique_id="u", width=1, height=1, file_size=1)
        ],
    )


def _install(monkeypatch) -> None:
    monkeypatch.setattr(rbac, "get_role", _get_role)
    monkeypatch.setattr(settings_store, "get_flags", _get_flags)
    monkeypatch.setattr(settings_store, "contains_badword", _no_badword)
    monkeypatch.setattr(groups_store, "upsert_group", _upsert)
    monkeypatch.setattr(Message, "delete", _msg_delete)
    monkeypatch.setattr(Message, "answer", _msg_answer)
    monkeypatch.setattr(Bot, "delete_message", _bot_delete_message)
    monkeypatch.setattr(Bot, "send_message", _bot_send_message)


def _feed(dp, bot, msg: Message) -> None:
    DELETES.clear()
    SENT.clear()
    asyncio.run(dp.feed_update(bot=bot, update=Update(update_id=_next(), message=msg)))


def test_is_bot_message_whitelist() -> None:
    human = _group_msg("x", user_id=1, is_bot=False)
    bot = _group_msg("x", user_id=BOT_ID, is_bot=True)
    assert textguards.is_bot_message(bot) is True
    assert textguards.is_bot_message(human) is False


def test_guards_never_moderate_a_bot_message(master, monkeypatch) -> None:
    """The glass panel is full of emoji - an ON emoji lock must not delete it."""
    _install(monkeypatch)
    bot, dp = master
    _feed(dp, bot, _group_msg("🛡 پنل مدیریت گروه ✅❌🔞", user_id=BOT_ID, is_bot=True))
    assert DELETES == [], "a bot-authored group message must never be deleted"
    assert SENT == [], "a bot-authored group message must never be answered"


def test_group_panel_survives_the_round_trip(master, monkeypatch) -> None:
    _install(monkeypatch)
    bot, dp = master

    _feed(dp, bot, _group_msg("پنل", user_id=ADMIN_ID, is_bot=False))
    assert SENT, "an admin writing پنل must get the glass panel"
    panel = SENT[0]
    assert "پنل مدیریت" in (panel.text or "")
    assert DELETES, "rule 6: the trigger message itself must disappear"

    # ... and the panel must not be re-moderated when it comes back as an update
    _feed(dp, bot, panel)
    assert DELETES == [], "the panel must never be deleted by the master's own locks"


def test_master_help_never_schedules_a_bot_message(master, monkeypatch) -> None:
    _install(monkeypatch)
    bot, dp = master
    _feed(dp, bot, _group_msg("/help", user_id=BOT_ID, is_bot=True))
    assert DELETES == []
    assert SENT == [], "a bot must not trigger the /help clean-chat protocol"


def test_worker_watchdog_never_deletes_a_bot_message(worker, monkeypatch) -> None:
    _install(monkeypatch)
    bot, dp = worker
    _feed(dp, bot, _photo_msg(user_id=BOT_ID, is_bot=True))
    assert DELETES == [], "the watchdog must never act on a bot's own media"
    _feed(dp, bot, _group_msg("🛡 پنل", user_id=BOT_ID, is_bot=True))
    assert DELETES == []
    assert SENT == []
