from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from abgguard.core import rbac, settings_store
from abgguard.core import trigger as trigger_store
from abgguard.core.rbac import Role
from abgguard.core.settings_store import GroupFlags
from abgguard.master import pv_keyboards as kb
from abgguard.master.handlers import panel as panel_mod
from abgguard.master.handlers import pv_panel as pv_mod
from abgguard.models.entities import DEFAULT_TRIGGER_NAME

ADMIN_ID = 6029973681
CHAT_ID = -100111222333
CUSTOM = "گزارش"

_uid = [700000]
DELETES: list[int] = []
SENT: list[Message] = []


def _next() -> int:
    _uid[0] += 1
    return _uid[0]


def _group_msg(text: str) -> Message:
    return Message(
        message_id=_next(),
        date=datetime.now(UTC),
        chat=Chat(id=CHAT_ID, type="supergroup", title="گروه تست"),
        from_user=User(id=ADMIN_ID, is_bot=False, first_name="ادمین"),
        text=text,
    )


async def _msg_delete(self, *args, **kwargs):
    DELETES.append(int(self.message_id))
    return True


async def _msg_answer(self, text=None, reply_markup=None, **kwargs):
    sent = Message(
        message_id=_next(),
        date=datetime.now(UTC),
        chat=self.chat,
        from_user=User(id=8837855460, is_bot=True, first_name="bot"),
        text=str(text),
        reply_markup=reply_markup,
    )
    SENT.append(sent)
    return sent


async def _get_role(chat_id: int, user_id: int) -> Role:
    return Role.ADMIN if user_id == ADMIN_ID else Role.USER


async def _can_open(chat_id: int, user_id: int) -> bool:
    return user_id == ADMIN_ID


async def _get_flags(chat_id: int) -> GroupFlags:
    return GroupFlags(chat_id=chat_id)


async def _upsert(*args, **kwargs) -> None:
    return None


def _install(monkeypatch) -> None:
    DELETES.clear()
    SENT.clear()
    monkeypatch.setattr(rbac, "get_role", _get_role)
    monkeypatch.setattr(rbac, "can_open_panel", _can_open)
    monkeypatch.setattr(settings_store, "get_flags", _get_flags)
    monkeypatch.setattr(
        "abgguard.master.handlers.panel.groups_store.upsert_group", _upsert
    )
    monkeypatch.setattr(Message, "delete", _msg_delete)
    monkeypatch.setattr(Message, "answer", _msg_answer)


def _feed(dp, bot, update: Update) -> None:
    DELETES.clear()
    SENT.clear()
    asyncio.run(dp.feed_update(bot=bot, update=update))


def _message_update(text: str) -> Update:
    return Update(update_id=_next(), message=_group_msg(text))


def _markup_has(markup, needle: str) -> bool:
    if markup is None:
        return False
    return any(needle in (btn.text or "") for row in markup.inline_keyboard for btn in row)


def _callback_datas(markup) -> list[str]:
    if markup is None:
        return []
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


# ---------------------------------------------------------------- unit ------
def test_validate_accepts_the_supported_range() -> None:
    assert trigger_store.validate("پنل") == "پنل"
    assert trigger_store.validate("  گزارش  ") == "گزارش"
    assert trigger_store.validate("a" * 20) == "a" * 20


@pytest.mark.parametrize("bad", ["a", "", "   ", "a" * 21, "/start"])
def test_validate_rejects_out_of_range_names(bad: str) -> None:
    with pytest.raises(ValueError):
        trigger_store.validate(bad)


def test_normalize_collapses_whitespace_and_zwnj() -> None:
    assert trigger_store.normalize("  نام   فراخوانی  ") == "نام فراخوانی"
    assert trigger_store.normalize("نام\u200cفاصله") == "نام فاصله"
    assert trigger_store.normalize(None) == ""


def test_matches_compares_against_the_group_alias(monkeypatch) -> None:
    async def _custom(chat_id: int) -> str:
        return CUSTOM

    monkeypatch.setattr(trigger_store, "get_trigger", _custom)

    async def _run() -> None:
        assert await trigger_store.matches(CHAT_ID, CUSTOM) is True
        assert await trigger_store.matches(CHAT_ID, f"  {CUSTOM}  ") is True
        assert await trigger_store.matches(CHAT_ID, "پنل") is False
        assert await trigger_store.matches(CHAT_ID, None) is False

    asyncio.run(_run())


def test_get_trigger_degrades_to_the_default_when_storage_is_down(
    monkeypatch,
) -> None:
    class _DeadRedis:
        def get_redis(self):
            raise RuntimeError("redis is down")

    class _DeadDB:
        @staticmethod
        def filter(**kwargs):
            raise RuntimeError("postgres is down")

    monkeypatch.setattr(trigger_store, "rk", _DeadRedis())
    monkeypatch.setattr(trigger_store, "Group", _DeadDB())

    assert asyncio.run(trigger_store.get_trigger(CHAT_ID)) == DEFAULT_TRIGGER_NAME


# ------------------------------------------------------- dispatcher level ----
def test_custom_alias_opens_the_panel_instead_of_the_default(master, monkeypatch) -> None:
    _install(monkeypatch)

    async def _custom(chat_id: int) -> str:
        return CUSTOM

    monkeypatch.setattr(trigger_store, "get_trigger", _custom)
    bot, dp = master

    _feed(dp, bot, _message_update(CUSTOM))
    assert SENT, "an admin writing the custom alias must get the glass panel"
    assert DELETES, "rule 6: the trigger message itself must disappear"
    assert _markup_has(SENT[0].reply_markup, CUSTOM), (
        "the panel button must show the current alias"
    )

    # the retired default word must no longer open the panel for this group
    _feed(dp, bot, _message_update("پنل"))
    assert SENT == [], "the retired default alias must not open the panel"
    assert DELETES == []


def test_default_alias_still_opens_the_panel(master, monkeypatch) -> None:
    _install(monkeypatch)
    bot, dp = master

    async def _default(chat_id: int) -> str:
        return DEFAULT_TRIGGER_NAME

    monkeypatch.setattr(trigger_store, "get_trigger", _default)

    _feed(dp, bot, _message_update("پنل"))
    assert SENT, "an admin writing پنل must get the glass panel"
    assert "پنل مدیریت" in (SENT[0].text or "")
    assert DELETES


# ------------------------------------------------------------ rename FSM -----
class _State:
    def __init__(self, data: dict | None = None) -> None:
        self._data = dict(data or {})
        self.cleared = False

    async def get_data(self) -> dict:
        return dict(self._data)

    async def clear(self) -> None:
        self.cleared = True
        self._data = {}

    async def update_data(self, **kwargs) -> None:
        self._data.update(kwargs)

    async def set_state(self, *args, **kwargs) -> None:
        return None


class _Bot:
    def __init__(self) -> None:
        self.edits: list[dict] = []

    async def edit_message_text(self, **kwargs) -> None:
        self.edits.append(kwargs)


def test_in_group_rename_saves_and_renders_the_new_alias(master, monkeypatch) -> None:
    _install(monkeypatch)
    saved: list[tuple[int, str]] = []

    async def _fake_set(chat_id: int, name: str) -> str:
        cleaned = trigger_store.validate(name)
        saved.append((chat_id, cleaned))
        return cleaned

    monkeypatch.setattr(trigger_store, "set_trigger", _fake_set)
    _bot, _dp = master
    stub = _Bot()
    state = _State({"menu_msg": 4242})

    asyncio.run(panel_mod.trigger_name_input(_group_msg("  راهنما  "), state, stub))

    assert saved == [(CHAT_ID, "راهنما")]
    assert state.cleared
    assert DELETES, "rule 6: the typed name must be removed from the group"
    assert len(stub.edits) == 1
    assert stub.edits[0]["message_id"] == 4242
    assert _markup_has(stub.edits[0]["reply_markup"], "راهنما")


def test_in_group_rename_keeps_the_panel_on_an_invalid_name(master, monkeypatch) -> None:
    _install(monkeypatch)
    notices: list[str] = []

    async def _fake_notice(message, text, **kwargs):
        notices.append(text)

    async def _boom(chat_id: int, name: str) -> str:
        raise ValueError("نام فراخوانی باید حداقل ۲ کاراکتر باشد.")

    monkeypatch.setattr(panel_mod, "notice", _fake_notice)
    monkeypatch.setattr(trigger_store, "set_trigger", _boom)
    _bot, _dp = master
    stub = _Bot()
    state = _State({"menu_msg": 4242})

    asyncio.run(panel_mod.trigger_name_input(_group_msg("x"), state, stub))

    assert notices, "the validation error must be shown to the admin"
    assert not state.cleared, "an invalid name must keep the FSM open for a retry"
    assert stub.edits == []


def test_rename_requires_the_panel_role(master, monkeypatch) -> None:
    _install(monkeypatch)

    async def _no(chat_id: int, user_id: int) -> bool:
        return False

    monkeypatch.setattr(rbac, "can_open_panel", _no)
    called: list[str] = []

    async def _boom(chat_id: int, name: str) -> str:
        called.append(name)
        return name

    monkeypatch.setattr(trigger_store, "set_trigger", _boom)
    _bot, _dp = master
    stub = _Bot()
    state = _State({"menu_msg": 1})

    asyncio.run(panel_mod.trigger_name_input(_group_msg("راهنما"), state, stub))

    assert called == [], "a member without the panel role must not rename the trigger"
    assert state.cleared


# ----------------------------------------------------------------- PV UI -----
def test_pv_group_panel_exposes_the_trigger_button() -> None:
    markup = kb.group_panel_kb(CHAT_ID, GroupFlags(chat_id=CHAT_ID), Role.ADMIN, "گزارش")
    datas = _callback_datas(markup)
    assert f"pv:admin:trg:{CHAT_ID}" in datas
    assert _markup_has(markup, "گزارش")
    assert _markup_has(markup, "نام فراخوانی ربات")


def test_pv_group_panel_button_defaults_to_the_stock_alias() -> None:
    markup = kb.group_panel_kb(CHAT_ID, GroupFlags(chat_id=CHAT_ID), Role.ADMIN)
    assert _markup_has(markup, DEFAULT_TRIGGER_NAME)


def test_pv_trigger_prompt_enters_the_staged_state(monkeypatch) -> None:
    """The PV button must open the FSM, never rename silently."""
    edits: list[tuple[str, object]] = []

    async def _require(cq, chat_id, min_role) -> Role:
        return Role.ADMIN

    async def _title(bot, chat_id) -> str:
        return "گروه تست"

    async def _ack(cq, text=None, alert=False):
        return None

    async def _edit(cq, text, markup):
        edits.append((text, markup))

    async def _get(chat_id: int) -> str:
        return CUSTOM

    monkeypatch.setattr(pv_mod, "_require_group", _require)
    monkeypatch.setattr(pv_mod, "_group_title", _title)
    monkeypatch.setattr(pv_mod, "_ack", _ack)
    monkeypatch.setattr(pv_mod, "_edit", _edit)
    monkeypatch.setattr(trigger_store, "get_trigger", _get)

    message = Message(
        message_id=_next(),
        date=datetime.now(UTC),
        chat=Chat(id=7, type="private"),
        from_user=User(id=ADMIN_ID, is_bot=False, first_name="ادمین"),
        text="menu",
    )
    cq = CallbackQuery(
        id="1",
        chat_instance="ci",
        from_user=User(id=ADMIN_ID, is_bot=False, first_name="ادمین"),
        data=f"pv:admin:trg:{CHAT_ID}",
        message=message,
    )

    seen: dict = {}

    class _Recorder:
        async def set_state(self, s):
            seen["state"] = s

        async def update_data(self, **kwargs):
            seen.update(kwargs)

        async def clear(self):
            seen["cleared"] = True

        async def get_data(self):
            return dict(seen)

    asyncio.run(pv_mod.pv_admin_callback(cq, _Recorder(), _Bot()))

    assert seen.get("state") is not None, "the rename flow must enter an FSM state"
    assert seen.get("target") == CHAT_ID
    assert seen.get("pv_chat") == 7
    assert seen.get("menu_msg") == message.message_id
    assert edits, "the prompt must be rendered in place (rule 6)"
    assert CUSTOM in edits[0][0], "the prompt must show the current alias"
    assert f"pv:admin:g:{CHAT_ID}" in _callback_datas(edits[0][1])
