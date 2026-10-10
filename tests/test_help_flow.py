from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from abgguard.core import rbac
from abgguard.core.rbac import Role
from abgguard.master.handlers import help as help_mod

_UPDATE_ID = 0


class Recorder:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.answers: list[tuple[str, object]] = []
        self.edits: list[tuple[str, object]] = []
        self.alerts: list[tuple[str | None, bool]] = []
        self.spawns: list[str] = []
        self.deleted: list[int] = []
        self.role: Role = Role.USER


RECORDER = Recorder()


def _uid() -> int:
    global _UPDATE_ID
    _UPDATE_ID += 1
    return _UPDATE_ID


def _chat(chat_type: str, chat_id: int) -> Chat:
    if chat_type == "private":
        return Chat(id=chat_id, type="private")
    return Chat(id=chat_id, type=chat_type, title="گروه تست")


def _message(
    text: str,
    *,
    chat_type: str = "supergroup",
    chat_id: int = -1001,
    uid: int = 7,
    message_id: int = 5,
) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=_chat(chat_type, chat_id),
        from_user=User(id=uid, is_bot=False, first_name="علی"),
        text=text,
    )


def _update_for_message(text: str, **kwargs) -> Update:
    return Update(update_id=_uid(), message=_message(text, **kwargs))


def _update_for_callback(data: str, *, uid: int = 7, chat_id: int = 7, message_id: int = 9) -> Update:
    message = _message("menu", chat_type="private", chat_id=chat_id, message_id=message_id)
    cq = CallbackQuery(
        id="1",
        chat_instance="ci",
        from_user=User(id=uid, is_bot=False, first_name="علی"),
        data=data,
        message=message,
    )
    return Update(update_id=_uid(), callback_query=cq)


def _callbacks(markup) -> list[str]:
    if markup is None:
        return []
    return [
        btn.callback_data
        for row in markup.inline_keyboard
        for btn in row
        if btn.callback_data
    ]


# ------------------------------------------------------------------ hooks ----
async def _msg_answer(self, text, reply_markup=None, **kwargs):
    RECORDER.answers.append((text, reply_markup))
    return _message(str(text), chat_type=self.chat.type, chat_id=self.chat.id)


async def _msg_edit(self, text, reply_markup=None, **kwargs):
    RECORDER.edits.append((text, reply_markup))
    return True


async def _msg_delete(self, **kwargs):
    RECORDER.deleted.append(self.message_id)
    return True


async def _cq_answer(self, text=None, show_alert=False, **kwargs):
    RECORDER.alerts.append((text, show_alert))
    return True


async def _state_clear(self, *args, **kwargs):
    return None


async def _bot_username(bot):
    return "abg_guard_bot"


async def _get_pv_role(user_id):
    return RECORDER.role


def _spawn(coro, *, name):
    RECORDER.spawns.append(name)
    coro.close()  # never awaited otherwise
    return None


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    RECORDER.reset()
    monkeypatch.setattr(Message, "answer", _msg_answer)
    monkeypatch.setattr(Message, "edit_text", _msg_edit)
    monkeypatch.setattr(Message, "delete", _msg_delete)
    monkeypatch.setattr(CallbackQuery, "answer", _cq_answer)
    monkeypatch.setattr(FSMContext, "clear", _state_clear)
    monkeypatch.setattr(help_mod, "_bot_username", _bot_username)
    monkeypatch.setattr(help_mod, "spawn", _spawn)
    monkeypatch.setattr(rbac, "get_pv_role", _get_pv_role)


def _feed(master, update: Update) -> None:
    bot, dp = master
    asyncio.run(dp.feed_update(bot=bot, update=update))


# ------------------------------------------------------------------ group ----
def test_group_help_replies_with_a_pv_button_and_self_destructs(master) -> None:
    _feed(master, _update_for_message("/help"))

    assert RECORDER.spawns == ["help-trigger", "help-reply"]
    assert RECORDER.answers, "group /help must answer"
    text, markup = RECORDER.answers[0]
    assert "پیوی" in text
    button = markup.inline_keyboard[0][0]
    assert button.url == "https://t.me/abg_guard_bot?start=help"
    assert button.text == help_mod.PV_LABEL
    assert help_mod.DELETE_AFTER == 10.0


def test_group_help_never_opens_the_full_menu(master) -> None:
    _feed(master, _update_for_message("/help"))
    assert RECORDER.edits == []
    assert "Level" not in RECORDER.answers[0][0]


# --------------------------------------------------------------------- PV ----
def test_private_help_opens_the_role_aware_menu(master) -> None:
    RECORDER.role = Role.ADMIN
    _feed(master, _update_for_message("/help", chat_type="private", chat_id=7))

    text, markup = RECORDER.answers[0]
    assert "Level 2" in text
    assert "ادمین گروه" in text
    callbacks = _callbacks(markup)
    assert "help:pg:pvdash" in callbacks
    assert "help:pg:subadmins" in callbacks
    assert "pv:root" in callbacks and "help:close" in callbacks


def test_private_help_for_a_plain_member_is_the_about_page_only(master) -> None:
    RECORDER.role = Role.USER
    _feed(master, _update_for_message("/help", chat_type="private", chat_id=7))

    text, markup = RECORDER.answers[0]
    assert "Level 0" in text
    assert _callbacks(markup) == ["help:pg:about", "pv:root", "help:close"]
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    assert help_mod.ADDBOT_LABEL in labels
    assert help_mod.SUPPORT_LABEL in labels


def test_the_group_button_deep_link_opens_the_full_menu(master) -> None:
    RECORDER.role = Role.SUBADMIN
    _feed(master, _update_for_message("/start help", chat_type="private", chat_id=7))

    text, markup = RECORDER.answers[0]
    assert "Level 1" in text
    assert "help:pg:law" in _callbacks(markup)


# --------------------------------------------------------------- callbacks ---
def test_section_callback_edits_the_same_message(master) -> None:
    RECORDER.role = Role.SUBADMIN
    _feed(master, _update_for_callback("help:pg:law"))

    assert RECORDER.edits, "help pages must be edited in place"
    text, markup = RECORDER.edits[0]
    assert "/mute" in text and "پاکسازی" in text
    assert _callbacks(markup) == ["help:home", "help:close"]


def test_a_section_above_the_users_level_is_denied(master) -> None:
    RECORDER.role = Role.USER
    _feed(master, _update_for_callback("help:pg:sys"))

    assert RECORDER.edits == []
    assert RECORDER.alerts[-1] == ("❌ دسترسی ندارید", True)


def test_pagination_walks_forward_and_back(master) -> None:
    RECORDER.role = Role.ADMIN

    _feed(master, _update_for_callback("help:idx:1"))
    assert "صفحهٔ ۲ از ۲" in RECORDER.edits[-1][0]

    _feed(master, _update_for_callback("help:home"))
    assert "صفحهٔ ۱ از ۲" in RECORDER.edits[-1][0]


def test_back_from_a_sub_page_returns_to_the_menu(master) -> None:
    RECORDER.role = Role.ADMIN
    _feed(master, _update_for_callback("help:home"))
    assert "صفحهٔ" in RECORDER.edits[-1][0]
    assert "help:pg:pvdash" in _callbacks(RECORDER.edits[-1][1])


def test_close_deletes_the_menu_message(master) -> None:
    _feed(master, _update_for_callback("help:close"))
    assert RECORDER.deleted == [9]


def test_malformed_callback_is_ignored(master) -> None:
    _feed(master, _update_for_callback("help:pg:nope"))
    assert RECORDER.edits == []
    assert RECORDER.alerts == [(None, False)]  # plain ack, no content
