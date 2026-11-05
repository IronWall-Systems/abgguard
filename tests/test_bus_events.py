from __future__ import annotations

import asyncio
import json
from typing import ClassVar

from aiogram.exceptions import TelegramBadRequest

from abgguard.services import moderation

NOTES: list[dict] = []
ESCALATIONS: list[dict] = []


def _notify(bot, chat_id, **kwargs):
    NOTES.append({"chat_id": chat_id, **kwargs})


def _escalate(bot, chat_id, **kwargs):
    ESCALATIONS.append({"chat_id": chat_id, **kwargs})


class _StubLog:
    rows: ClassVar[list[dict]] = []

    @classmethod
    async def create(cls, **kwargs) -> None:
        cls.rows.append(kwargs)


def _run(
    monkeypatch,
    reason: str,
    *,
    user_id: int = 10,
    me_id: int = 1,
    actor_id: int | None = None,
    actor_source: str | None = None,
    target_name: str | None = None,
    message_id: int | None = None,
) -> tuple[list, list]:
    calls: list[tuple] = []

    async def _mute(bot, chat_id, uid, seconds):
        calls.append(("mute", chat_id, uid, seconds))
        return True

    async def _ban(bot, chat_id, uid):
        calls.append(("ban", chat_id, uid))
        return True

    async def _perm_mute(bot, chat_id, uid):
        calls.append(("perm_mute", chat_id, uid))
        return True

    async def _delete(bot, chat_id, mid):
        calls.append(("delete", chat_id, mid))
        return True

    monkeypatch.setattr(moderation, "mute", _mute)
    monkeypatch.setattr(moderation, "ban", _ban)
    monkeypatch.setattr(moderation, "perm_mute", _perm_mute)
    monkeypatch.setattr(moderation, "safe_delete", _delete)
    _StubLog.rows = []
    monkeypatch.setattr(moderation, "BanLog", _StubLog)
    NOTES.clear()
    ESCALATIONS.clear()
    monkeypatch.setattr(moderation.modlog, "notify", _notify)
    monkeypatch.setattr(moderation.modlog, "escalate", _escalate)

    payload = {"chat_id": -100, "user_id": user_id, "reason": reason}
    if actor_id is not None:
        payload["actor_id"] = actor_id
    if actor_source is not None:
        payload["actor_source"] = actor_source
    if target_name is not None:
        payload["target_name"] = target_name
    if message_id is not None:
        payload["message_id"] = message_id
    raw = json.dumps(payload)
    asyncio.run(moderation.handle_ban_event(None, raw, me_id))
    return calls, list(_StubLog.rows)


def test_mute_event_restricts_instead_of_banning(monkeypatch) -> None:
    """Regression: `mute:*` events used to fall through to a permanent ban."""
    calls, rows = _run(monkeypatch, "mute:قفل لینک:300s")
    assert calls == [("mute", -100, 10, 300)]
    assert rows[0]["reason"] == "mute:قفل لینک:300s"


def test_mute_reason_parses_from_the_right(monkeypatch) -> None:
    calls, _ = _run(monkeypatch, "mute:کلمه رکیک (bad):600s")
    assert calls == [("mute", -100, 10, 600)]


def test_delete_only_event_never_mutes(monkeypatch) -> None:
    """Profanity strikes 1-4 publish `deleteonly:` - delete, never restrict."""
    calls, rows = _run(
        monkeypatch, "deleteonly:کلمه رکیک (bad)", message_id=42
    )
    assert calls == [("delete", -100, 42)]
    assert rows[0]["reason"] == "کلمه رکیک (bad)"
    assert NOTES[0]["action"] == "حذف پیام"
    assert ESCALATIONS == []


def test_plain_reason_mutes_forever_instead_of_banning(monkeypatch) -> None:
    """A ban-worthy infraction is a permanent mute - never a `ban_chat_member`."""
    calls, rows = _run(monkeypatch, "پورن")
    assert calls == [("perm_mute", -100, 10)]
    assert rows[0]["reason"] == "پورن"


def test_a_ban_worthy_infraction_is_escalated_to_the_owner(monkeypatch) -> None:
    _run(monkeypatch, "پورن", target_name="Ali")

    assert ESCALATIONS == [{
        "chat_id": -100,
        "target": 10,
        "reason": "پورن",
        "target_name": "Ali",
    }]


PORN_REASON = "permamute:ارسال محتوای مستهجن (Pornographic Media)"


def test_permanent_mute_replaces_the_ban_for_porn(monkeypatch) -> None:
    """A content violation silences the offender instead of removing them."""
    calls, rows = _run(monkeypatch, PORN_REASON, actor_source="watchdog")

    assert calls == [("perm_mute", -100, 10)]
    assert rows[0]["reason"] == "ارسال محتوای مستهجن (Pornographic Media)"
    assert ESCALATIONS[0]["reason"] == "ارسال محتوای مستهجن (Pornographic Media)"


def test_the_content_report_carries_the_numeric_id(monkeypatch) -> None:
    _run(monkeypatch, PORN_REASON, actor_source="watchdog", target_name="Ali")

    assert NOTES[0]["action"] == "سکوت دائم (Permanent Mute)"
    assert NOTES[0]["template"] == "content"
    assert NOTES[0]["target"] == 10
    assert NOTES[0]["target_name"] == "Ali"
    assert NOTES[0]["reason"] == "ارسال محتوای مستهجن (Pornographic Media)"


def test_a_regular_saction_keeps_the_standard_card(monkeypatch) -> None:
    _run(monkeypatch, "پورن")

    assert "template" not in NOTES[0]


def test_the_bot_never_punishes_itself(monkeypatch) -> None:
    calls, rows = _run(monkeypatch, "پورن", user_id=1, me_id=1)
    assert calls == []
    assert rows == []


def test_the_actor_ride_along_with_the_event(monkeypatch) -> None:
    """The private mod-log must know *who* ordered the sanction."""
    _run(monkeypatch, "پورن", actor_id=7, actor_source="admin")
    assert NOTES == [{
        "chat_id": -100,
        "action": "سکوت دائم (Permanent Mute)",
        "target": 10,
        "actor": 7,
        "actor_source": "admin",
        "reason": "پورن",
    }]


def test_a_timed_mute_never_reaches_the_owner(monkeypatch) -> None:
    """Only ban-worthy infractions are escalated - a short mute is routine."""
    _run(monkeypatch, "mute:قفل لینک:300s")
    assert ESCALATIONS == []


def test_an_automatic_guard_has_no_actor(monkeypatch) -> None:
    _run(monkeypatch, "mute:قفل لینک:300s")
    assert NOTES[0]["actor"] is None
    assert NOTES[0]["actor_source"] is None
    assert NOTES[0]["action"] == "سکوت 300 ثانیه"


def test_the_master_bot_is_named_as_the_executor(monkeypatch) -> None:
    _run(monkeypatch, "پورن", actor_source="master")
    assert NOTES[0]["actor_source"] == "master"


def test_the_watchdog_bot_is_named_as_the_executor(monkeypatch) -> None:
    _run(monkeypatch, "پورن", actor_source="watchdog")
    assert NOTES[0]["actor_source"] == "watchdog"


def test_malformed_payload_is_dropped(monkeypatch) -> None:
    calls = []

    async def _fail(*args, **kwargs):  # pragma: no cover - must never run
        calls.append(args)

    monkeypatch.setattr(moderation, "mute", _fail)
    monkeypatch.setattr(moderation, "ban", _fail)
    asyncio.run(moderation.handle_ban_event(None, "not json", 1))
    assert calls == []


def test_the_offending_message_is_deleted_with_the_sanction(monkeypatch) -> None:
    """Porn / severe bad word: the message goes away next to the permanent mute."""
    calls, _ = _run(monkeypatch, PORN_REASON, message_id=42, actor_source="watchdog")

    assert calls[0] == ("delete", -100, 42), "deletion comes first"
    assert ("perm_mute", -100, 10) in calls
    assert not any(c[0] == "ban" for c in calls)


def test_an_event_without_a_message_id_still_applies_the_sanction(monkeypatch) -> None:
    calls, _ = _run(monkeypatch, PORN_REASON)

    assert calls == [("perm_mute", -100, 10)]


def test_a_failing_delete_never_blocks_the_penalty() -> None:
    """`safe_delete()` is awaited *before* every sanction - it may not raise."""
    class _BoomBot:
        async def delete_message(self, *args, **kwargs):
            raise RuntimeError("telegram exploded")

    assert asyncio.run(moderation.safe_delete(_BoomBot(), -100, 5)) is False

    class _RefusedBot:
        async def delete_message(self, *args, **kwargs):
            raise TelegramBadRequest(method=None, message="message can't be deleted")

    assert asyncio.run(moderation.safe_delete(_RefusedBot(), -100, 5)) is False
