from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from aiogram.types import CallbackQuery, Chat, Message, User

from abgguard.core import rbac, settings_store
from abgguard.core import trigger as trigger_store
from abgguard.core.rbac import Role
from abgguard.core.settings_store import GroupFlags
from abgguard.master import keyboards as gkb
from abgguard.master.handlers import panel as panel_mod
from abgguard.master.states import PanelSG

GOD_ID = 6029973681
CHAT_ID = -100111222333

_uid = [900000]
DELETES: list[int] = []
EDITS: list[dict] = []
ACKS: list[tuple] = []


def _next() -> int:
    _uid[0] += 1
    return _uid[0]


class _State:
    def __init__(self) -> None:
        self.state = None
        self.data: dict = {}

    async def set_state(self, s) -> None:
        self.state = s

    async def get_state(self):
        return self.state

    async def update_data(self, **kwargs) -> None:
        self.data.update(kwargs)

    async def get_data(self) -> dict:
        return dict(self.data)

    async def clear(self) -> None:
        self.state = None
        self.data = {}


def _panel_msg() -> Message:
    return Message(
        message_id=_next(),
        date=datetime.now(UTC),
        chat=Chat(id=CHAT_ID, type="supergroup", title="گروه تست"),
        from_user=User(id=8837855460, is_bot=True, first_name="bot"),
        text="🛡 پنل مدیریت گروه",
    )


def _callback(data: str, *, uid: int = GOD_ID) -> CallbackQuery:
    return CallbackQuery(
        id="1",
        chat_instance="ci",
        from_user=User(id=uid, is_bot=False, first_name="ادمین"),
        data=data,
        message=_panel_msg(),
    )


async def _msg_delete(self, *args, **kwargs):
    DELETES.append(int(self.message_id))
    return True


async def _msg_edit(self, text, reply_markup=None, **kwargs):
    EDITS.append({"text": text, "markup": reply_markup})
    return True


async def _msg_delete_boom(self, *args, **kwargs):
    raise RuntimeError("message can't be deleted")


async def _ack(cq, text=None, alert=False):
    ACKS.append((text, alert))


async def _get_role(chat_id, user_id) -> Role:
    return Role.ADMIN if user_id == GOD_ID else Role.USER


async def _flags(chat_id: int) -> GroupFlags:
    return GroupFlags(chat_id=chat_id)


async def _trigger(chat_id: int) -> str:
    return "پنل"


def _install(monkeypatch, *, delete=_msg_delete) -> _State:
    DELETES.clear()
    EDITS.clear()
    ACKS.clear()
    monkeypatch.setattr(rbac, "get_role", _get_role)
    monkeypatch.setattr(settings_store, "get_flags", _flags)
    monkeypatch.setattr(trigger_store, "get_trigger", _trigger)
    monkeypatch.setattr(panel_mod, "_ack", _ack)
    monkeypatch.setattr(Message, "delete", delete)
    monkeypatch.setattr(Message, "edit_text", _msg_edit)
    return _State()


def _run(cq, state) -> None:
    asyncio.run(panel_mod.on_panel_callback(cq, state, bot=None))


def _datas(markup) -> list[str]:
    if markup is None:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _rows(markup) -> list[list[str]]:
    return [[b.callback_data for b in row] for row in markup.inline_keyboard]


# ------------------------------------------------------- always visible ------
def test_the_main_panel_always_ends_with_the_close_button() -> None:
    for role in (Role.USER, Role.SUBADMIN, Role.ADMIN, Role.GOD):
        markup = gkb.panel_kb(GroupFlags(chat_id=CHAT_ID), role)
        datas = _datas(markup)
        assert gkb.CLOSE_DATA in datas, f"missing close button for {role}"
        assert markup.inline_keyboard[-1][0].callback_data == gkb.CLOSE_DATA, (
            "the closer must be the last row, never hidden below the fold"
        )
        assert markup.inline_keyboard[-1][0].text == gkb.CLOSE_LABEL


def test_every_group_panel_screen_keeps_the_close_button() -> None:
    screens = {
        "panel": gkb.panel_kb(GroupFlags(chat_id=CHAT_ID), Role.ADMIN),
        "panel-subadmin": gkb.panel_kb(GroupFlags(chat_id=CHAT_ID), Role.SUBADMIN),
        "badwords-empty": gkb.badwords_menu_kb([]),
        "badwords-full": gkb.badwords_menu_kb(["foo", "bar", "baz"]),
        "subadmins-empty": gkb.subadmin_menu_kb([]),
        "subadmins-full": gkb.subadmin_menu_kb([1, 2, 3]),
        "trigger-input": gkb.trigger_menu_kb(),
    }
    for name, markup in screens.items():
        datas = _datas(markup)
        assert gkb.CLOSE_DATA in datas, f"{name} lost its close button"
        assert markup.inline_keyboard[-1][0].callback_data == gkb.CLOSE_DATA, name


def test_the_close_label_is_readable_persian() -> None:
    assert "بستن پنل" in gkb.CLOSE_LABEL


def test_navigation_buttons_are_preserved() -> None:
    datas = _datas(gkb.panel_kb(GroupFlags(chat_id=CHAT_ID), Role.ADMIN))
    for expected in ("abg:bad", "abg:sa", "abg:trg", "abg:refresh", "abg:help"):
        assert expected in datas
    locks = [d for d in datas if d.startswith("abg:lock:")]
    assert len(locks) == len(gkb.TOGGLES)


# ------------------------------------------------------------ behaviour -----
def test_an_admin_close_deletes_the_panel_message(monkeypatch) -> None:
    state = _install(monkeypatch)

    _run(_callback("abg:close"), state)

    assert len(DELETES) == 1, "the panel must disappear from the group (rule 6)"
    assert state.state is None, "a pending input state must not survive the close"
    assert ACKS and ACKS[0][0] is not None


def test_close_clears_a_pending_input_state(monkeypatch) -> None:
    state = _install(monkeypatch)
    state.state = PanelSG.bad_word
    state.data = {"menu_msg": 123}

    _run(_callback("abg:close"), state)

    assert state.state is None
    assert state.data == {}
    assert len(DELETES) == 1


def test_a_plain_member_cannot_close_the_panel(monkeypatch) -> None:
    state = _install(monkeypatch)

    _run(_callback("abg:close", uid=555), state)

    assert DELETES == [], "only staff may dismiss the panel"
    assert ACKS == [("❌ دسترسی ندارید", True)]


def test_a_failed_delete_falls_back_to_an_inert_message(monkeypatch) -> None:
    state = _install(monkeypatch, delete=_msg_delete_boom)

    _run(_callback("abg:close"), state)

    assert DELETES == []
    assert EDITS and EDITS[0]["markup"] is None
    assert "بسته شد" in EDITS[0]["text"]


def test_close_leaves_the_rest_of_the_panel_untouched(monkeypatch) -> None:
    state = _install(monkeypatch)

    _run(_callback("abg:close"), state)
    assert EDITS == [], "a successful close must not rewrite anything"

    DELETES.clear()
    _run(_callback("abg:refresh"), state)
    assert DELETES == [], "refresh must not delete the panel"
