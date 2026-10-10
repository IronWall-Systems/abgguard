from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from aiogram.types import Chat, Message, Update, User

from abgguard.core import rbac
from abgguard.core.rbac import Role
from abgguard.master.handlers import pv_panel

GOD_ID = 6029973681
_UPDATE_ID = 70000

ANSWERS: list[tuple[str, object]] = []


def _uid() -> int:
    global _UPDATE_ID
    _UPDATE_ID += 1
    return _UPDATE_ID


async def _answer(self, text=None, reply_markup=None, **kwargs):
    ANSWERS.append((str(text), reply_markup))
    return self


async def _get_pv_role(user_id: int) -> Role:
    return Role.GOD if user_id == GOD_ID else Role.USER


async def _managed(user_id: int):
    return []


def _callbacks(markup) -> list[str]:
    if markup is None:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def _pv_message(text: str) -> Message:
    return Message(
        message_id=_uid(),
        date=datetime.now(UTC),
        chat=Chat(id=GOD_ID, type="private"),
        from_user=User(id=GOD_ID, is_bot=False, first_name="خدا"),
        text=text,
    )


def _install(monkeypatch) -> None:
    monkeypatch.setattr(Message, "answer", _answer)
    monkeypatch.setattr(rbac, "get_pv_role", _get_pv_role)
    monkeypatch.setattr(rbac, "list_managed_chats", _managed)


def _feed(master, text: str) -> list[tuple[str, object]]:
    bot, dp = master
    ANSWERS.clear()
    update = Update(update_id=_uid(), message=_pv_message(text))
    asyncio.run(dp.feed_update(bot=bot, update=update))
    return list(ANSWERS)


def test_start_opens_the_god_dashboard(master, monkeypatch) -> None:
    _install(monkeypatch)
    answers = _feed(master, "/start")

    assert answers, "the God Admin must get a reply to /start in PV"
    text, markup = answers[0]
    assert "Level 3" in text
    assert "pv:god:sys" in _callbacks(markup)


def test_panel_opens_the_god_dashboard(master, monkeypatch) -> None:
    _install(monkeypatch)
    answers = _feed(master, "/panel")

    assert answers, "the God Admin must get a reply to /panel in PV"
    text, markup = answers[0]
    assert "Level 3" in text
    assert "pv:god:sys" in _callbacks(markup)


def test_the_persian_trigger_opens_the_god_dashboard(master, monkeypatch) -> None:
    _install(monkeypatch)
    answers = _feed(master, "پنل")

    assert answers, "the God Admin must get a reply to the «پنل» trigger in PV"
    assert "Level 3" in answers[0][0]


def test_plain_text_in_pv_is_never_silent(master, monkeypatch) -> None:
    _install(monkeypatch)
    answers = _feed(master, "سلام")

    assert answers, "plain PV text must not be swallowed"


def test_the_pv_routers_are_mounted_on_the_master_dispatcher(master) -> None:
    _, dp = master
    names = [r.name for r in dp.sub_routers]
    assert names[:4] == [
        "master.help",
        "master.pv-fsm",
        "master.pv",
        "master.rbac",
    ], names
    assert pv_panel.router.name == "master.pv"


def test_the_worker_dispatcher_has_no_pv_routers(worker) -> None:
    _, dp = worker
    names = [r.name for r in dp.sub_routers]
    assert names == ["worker.media"], names
    assert not any(name.startswith("master.") for name in names)


# ------------------------------------------------------------------ drops ----
def test_an_unmatched_update_is_logged_as_dropped(master, caplog) -> None:
    import logging

    caplog.set_level(logging.WARNING)
    bot, dp = master
    update = Update(
        update_id=_uid(),
        edited_message=Message(
            message_id=_uid(),
            date=datetime.now(UTC),
            chat=Chat(id=GOD_ID, type="private"),
            from_user=User(id=GOD_ID, is_bot=False, first_name="خدا"),
            text="ویرایش شد",
        ),
    )
    asyncio.run(dp.feed_update(bot=bot, update=update))

    assert any("UPDATE DROPPED" in r.message for r in caplog.records), [
        r.message for r in caplog.records
    ]
    assert any("reason=no handler matched" in r.message for r in caplog.records)


def test_a_pv_update_from_the_god_admin_is_traced(
    master, monkeypatch, caplog
) -> None:
    import logging

    _install(monkeypatch)
    caplog.set_level(logging.INFO)
    _feed(master, "/start")

    assert any(
        "PV trace" in r.message and str(GOD_ID) in r.message for r in caplog.records
    ), [r.message for r in caplog.records]


def test_a_handler_crash_is_logged_and_never_leaves_the_user_silent(
    master, monkeypatch, caplog
) -> None:
    import logging

    from aiogram import Bot

    caplog.set_level(logging.ERROR)
    sent: list[tuple[int, str]] = []

    async def _send(self, chat_id, text, **kwargs):
        sent.append((chat_id, str(text)))
        return self

    def _boom(*args, **kwargs):
        raise RuntimeError("boom: dashboard exploded")

    monkeypatch.setattr(Bot, "send_message", _send)
    monkeypatch.setattr(pv_panel, "_open_pv", _boom)

    _feed(master, "/start")

    assert any("UPDATE ERROR" in r.message for r in caplog.records), [
        r.message for r in caplog.records
    ]
    assert any(
        "boom: dashboard exploded" in r.message for r in caplog.records
    ), [r.message for r in caplog.records]
    assert sent and sent[0][0] == GOD_ID, "the user must get an error notice, not silence"
