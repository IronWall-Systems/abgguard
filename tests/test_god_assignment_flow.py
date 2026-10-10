from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from aiogram.types import CallbackQuery, Chat, Message, User

from abgguard.core import rbac
from abgguard.master import pv_keyboards as kb
from abgguard.master.handlers import pv_panel as pv_mod

GOD_ID = 6029973681
CHAT_ID = -100123456789
USER_ID = 123456789

_uid = [800000]
DELETES: list[int] = []


def _next() -> int:
    _uid[0] += 1
    return _uid[0]


class _State:
    def __init__(self, data: dict | None = None) -> None:
        self._data = dict(data or {})
        self.state = None
        self.cleared = False

    async def set_state(self, s) -> None:
        self.state = s

    async def get_state(self):
        return self.state

    async def update_data(self, **kwargs) -> None:
        self._data.update(kwargs)

    async def get_data(self) -> dict:
        return dict(self._data)

    async def clear(self) -> None:
        self.cleared = True
        self.state = None
        self._data = {}


class _Bot:
    def __init__(self) -> None:
        self.edits: list[dict] = []

    async def edit_message_text(self, **kwargs) -> None:
        self.edits.append(kwargs)


def _pv_message(text: str) -> Message:
    return Message(
        message_id=_next(),
        date=datetime.now(UTC),
        chat=Chat(id=GOD_ID, type="private"),
        from_user=User(id=GOD_ID, is_bot=False, first_name="گاد"),
        text=text,
    )


def _callback(data: str) -> CallbackQuery:
    return CallbackQuery(
        id="1",
        chat_instance="ci",
        from_user=User(id=GOD_ID, is_bot=False, first_name="گاد"),
        data=data,
        message=_pv_message("menu"),
    )


async def _msg_delete(self, *args, **kwargs):
    DELETES.append(int(self.message_id))
    return True


def _markup_datas(markup) -> list[str]:
    if markup is None:
        return []
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


def _markup_has(markup, needle: str) -> bool:
    if markup is None:
        return False
    return any(needle in (btn.text or "") for row in markup.inline_keyboard for btn in row)


def _install(monkeypatch, edits: list[tuple[str, object]]) -> _State:
    """Patch every side effect the God Admin flow touches."""
    DELETES.clear()

    async def _is_god(user_id: int) -> bool:
        return user_id == GOD_ID

    async def _ack(cq, text=None, alert=False):
        return None

    async def _edit(cq, text, markup):
        edits.append((text, markup))

    async def _edit_menu(bot, data, text, markup, **kwargs):
        edits.append((text, markup))

    async def _owners_text(note: str | None = None) -> str:
        return f"لیست مدیران{': ' + note if note else ''}"

    async def _owners_rows():
        return [(CHAT_ID, USER_ID, "گروه تست")]

    monkeypatch.setattr(pv_mod, "rbac", _fake_rbac(monkeypatch))
    monkeypatch.setattr(rbac, "is_god", _is_god)
    monkeypatch.setattr(pv_mod, "_ack", _ack)
    monkeypatch.setattr(pv_mod, "_edit", _edit)
    monkeypatch.setattr(pv_mod, "_edit_menu", _edit_menu)
    monkeypatch.setattr(pv_mod, "_owners_text", _owners_text)
    monkeypatch.setattr(pv_mod, "_owners_rows", _owners_rows)
    monkeypatch.setattr(Message, "delete", _msg_delete)
    return _State({"pv_chat": GOD_ID, "menu_msg": 55})


def _fake_rbac(monkeypatch):
    """The real rbac module with only the two grant writers recorded."""
    return rbac


# ------------------------------------------------------------------ steps ----
def test_step1_prompts_for_the_group_id_first(monkeypatch) -> None:
    edits: list[tuple[str, object]] = []
    state = _install(monkeypatch, edits)

    asyncio.run(pv_mod.pv_god_callback(_callback("pv:god:ow:add"), state, _Bot()))

    assert state.state is not None, "step 1 must enter the staged FSM"
    assert edits, "the prompt must edit the dashboard in place (rule 6)"
    text = edits[0][0]
    assert "مرحله ۱ از ۳" in text
    assert "-1001234567890" in text
    assert "pv:god:owners" in _markup_datas(edits[0][1])


def test_step1_rejects_a_non_group_id(monkeypatch) -> None:
    edits: list[tuple[str, object]] = []
    state = _install(monkeypatch, edits)
    notices: list[str] = []

    async def _notice(message, text, **kwargs):
        notices.append(text)

    monkeypatch.setattr(pv_mod, "_notice", _notice)
    state.state = pv_mod.PvGodSG.owner_chat

    asyncio.run(pv_mod.pv_owner_chat_input(_pv_message("12345"), state, _Bot()))

    assert notices and "منفی" in notices[0], "a positive id is not a group id"
    assert state.state is pv_mod.PvGodSG.owner_chat, "the step must be retried"
    assert state._data.get("target_chat") is None
    assert edits == []


def test_step2_is_reached_with_a_valid_group_id(monkeypatch) -> None:
    edits: list[tuple[str, object]] = []
    state = _install(monkeypatch, edits)
    state.state = pv_mod.PvGodSG.owner_chat

    asyncio.run(pv_mod.pv_owner_chat_input(_pv_message(str(CHAT_ID)), state, _Bot()))

    assert state.state is pv_mod.PvGodSG.owner_user
    assert state._data["target_chat"] == CHAT_ID
    assert DELETES, "rule 6: the typed id must disappear from the PV"
    assert len(edits) == 1
    assert "مرحله ۲ از ۳" in edits[0][0]
    assert str(CHAT_ID) in edits[0][0]


def test_step3_offers_both_levels_for_the_exact_pair(monkeypatch) -> None:
    edits: list[tuple[str, object]] = []
    state = _install(monkeypatch, edits)
    state.state = pv_mod.PvGodSG.owner_user
    state._data["target_chat"] = CHAT_ID

    asyncio.run(pv_mod.pv_owner_user_input(_pv_message(str(USER_ID)), state, _Bot()))

    assert state.state is pv_mod.PvGodSG.owner_level
    assert state._data["target_user"] == USER_ID
    assert len(edits) == 1
    assert "مرحله ۳ از ۳" in edits[0][0]
    datas = _markup_datas(edits[0][1])
    assert f"pv:god:ow:lvl:2:{USER_ID}:{CHAT_ID}" in datas
    assert f"pv:god:ow:lvl:1:{USER_ID}:{CHAT_ID}" in datas, (
        "the God Admin must be able to grant Level 1 (owner-approved §3 exception)"
    )


def test_step2_rejects_a_non_numeric_user(monkeypatch) -> None:
    edits: list[tuple[str, object]] = []
    state = _install(monkeypatch, edits)
    notices: list[str] = []

    async def _notice(message, text, **kwargs):
        notices.append(text)

    monkeypatch.setattr(pv_mod, "_notice", _notice)
    state.state = pv_mod.PvGodSG.owner_user
    state._data["target_chat"] = CHAT_ID

    asyncio.run(pv_mod.pv_owner_user_input(_pv_message("-5"), state, _Bot()))

    assert notices, "a negative id is not a Telegram user id"
    assert state.state is pv_mod.PvGodSG.owner_user
    assert edits == []


def test_typed_answer_at_step3_grants_nothing(monkeypatch) -> None:
    edits: list[tuple[str, object]] = []
    state = _install(monkeypatch, edits)
    notices: list[str] = []
    grants: list[tuple] = []

    async def _notice(message, text, **kwargs):
        notices.append(text)

    async def _no_grant(*args, **kwargs):
        grants.append(args)

    monkeypatch.setattr(pv_mod, "_notice", _notice)
    monkeypatch.setattr(rbac, "add_admin", _no_grant)
    monkeypatch.setattr(rbac, "add_subadmin", _no_grant)
    state.state = pv_mod.PvGodSG.owner_level

    asyncio.run(pv_mod.pv_owner_level_hint(_pv_message("2"), state))

    assert notices and "دکمه" in notices[0]
    assert grants == [], "level 3 must only be granted through the buttons"


# -------------------------------------------------------------- the grant ----
def _grant(monkeypatch, level: str) -> list[tuple]:
    edits: list[tuple[str, object]] = []
    _install(monkeypatch, edits)
    calls: list[tuple] = []

    async def _add_admin(chat_id, user_id, granted_by):
        calls.append(("admin", chat_id, user_id, granted_by))

    async def _add_sub(chat_id, user_id, granted_by):
        calls.append(("sub", chat_id, user_id, granted_by))

    monkeypatch.setattr(rbac, "add_admin", _add_admin)
    monkeypatch.setattr(rbac, "add_subadmin", _add_sub)

    data = f"pv:god:ow:lvl:{level}:{USER_ID}:{CHAT_ID}"
    asyncio.run(pv_mod.pv_god_callback(_callback(data), _State(), _Bot()))
    return calls


def test_level2_button_grants_the_owner_role(monkeypatch) -> None:
    calls = _grant(monkeypatch, "2")
    assert calls == [("admin", CHAT_ID, USER_ID, GOD_ID)]


def test_level1_button_grants_the_subadmin_role(monkeypatch) -> None:
    calls = _grant(monkeypatch, "1")
    assert calls == [("sub", CHAT_ID, USER_ID, GOD_ID)]


def test_malformed_level_callback_is_ignored(monkeypatch) -> None:
    calls = _grant(monkeypatch, "9")
    assert calls == [], "an unknown level must never reach the RBAC writers"


def test_level_keyboard_layout() -> None:
    markup = kb.god_owner_level_kb(USER_ID, CHAT_ID)
    datas = _markup_datas(markup)
    assert f"pv:god:ow:lvl:2:{USER_ID}:{CHAT_ID}" in datas
    assert f"pv:god:ow:lvl:1:{USER_ID}:{CHAT_ID}" in datas
    assert "pv:god:owners" in datas
    # callback data must stay inside Telegram's 64-byte budget
    assert all(len(d.encode()) <= 64 for d in datas)
    assert _markup_has(markup, "Level 2")
    assert _markup_has(markup, "Level 1")
