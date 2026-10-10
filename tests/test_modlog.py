from __future__ import annotations

import asyncio

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from abgguard.core import rbac
from abgguard.services import modlog

CHAT_ID = -1001


class FakeBot:
    """Records DMs; per-recipient failures are configured through ``fail``."""

    def __init__(self, *, fail: dict[int, Exception] | None = None, flood_once=False) -> None:
        self.sent: list[tuple[int, str, str | None]] = []
        self.fail = dict(fail or {})
        self.flood_once = flood_once
        self._flooded = False

    async def send_message(self, chat_id, text, **kwargs):
        if self.flood_once and not self._flooded:
            self._flooded = True
            raise TelegramRetryAfter(method=None, message="retry after", retry_after=0)
        exc = self.fail.get(chat_id)
        if exc is not None:
            raise exc
        self.sent.append((chat_id, text, kwargs.get("parse_mode")))
        return None


def _install_roster(monkeypatch, owners: list[int], subs: list[int]) -> None:
    async def _admins(chat_id):
        return list(owners)

    async def _subadmins(chat_id):
        return list(subs)

    monkeypatch.setattr(rbac, "list_admins", _admins)
    monkeypatch.setattr(rbac, "list_subadmins", _subadmins)


# --------------------------------------------------------------- roster ------
def test_roster_merges_owners_then_subadmins_without_duplicates(monkeypatch) -> None:
    _install_roster(monkeypatch, [7, 9], [9, 11])

    assert asyncio.run(modlog.recipients(CHAT_ID)) == [7, 9, 11]


def test_a_broken_roster_degrades_to_zero_recipients(monkeypatch) -> None:
    async def _boom(chat_id):
        raise RuntimeError("database is not initialized")

    monkeypatch.setattr(rbac, "list_admins", _boom)
    monkeypatch.setattr(rbac, "list_subadmins", _boom)

    assert asyncio.run(modlog.recipients(CHAT_ID)) == []


def test_the_roster_is_capped(monkeypatch) -> None:
    _install_roster(monkeypatch, list(range(200)), [])

    got = asyncio.run(modlog.recipients(CHAT_ID))
    assert len(got) == modlog.MAX_RECIPIENTS


# --------------------------------------------------------------- render ------
def test_the_card_names_every_part_of_the_saction() -> None:
    text = modlog.render(
        chat_id=CHAT_ID, action="بن", target=42, actor=7, reason="پورن"
    )

    assert "📜 لاگ اقدام امنیتی" in text
    assert f"<code>{CHAT_ID}</code>" in text
    assert "<code>7</code>" in text
    assert "<code>42</code>" in text
    assert "🔹 اقدام: بن" in text
    assert "ℹ️ دلیل: پورن" in text


def test_the_card_is_html_escaped() -> None:
    text = modlog.render(
        chat_id=CHAT_ID, action="a<b>&c", target=1, reason='x"y'
    )

    assert "a&lt;b&gt;&amp;c" in text
    assert "<b>" not in text
    assert "x&quot;y" in text  # html.escape() also defuses quotes


def test_an_automatic_saction_is_labelled_as_system() -> None:
    text = modlog.render(chat_id=CHAT_ID, action="بن", target=1, actor=None)

    assert "سیستم/ربات" in text


def test_the_master_bot_is_named_on_the_card() -> None:
    text = modlog.render(
        chat_id=CHAT_ID, action="بن", target=1, actor=None, actor_source="master"
    )

    assert "ربات اصلی (Master)" in text
    assert "سیستم/ربات" not in text


def test_the_watchdog_bot_is_named_on_the_card() -> None:
    text = modlog.render(
        chat_id=CHAT_ID, action="بن", target=1, actor=None, actor_source="watchdog"
    )

    assert "ربات نگهبان (Watchdog)" in text
    assert "سیستم/ربات" not in text


def test_a_human_admin_is_named_by_id_and_role() -> None:
    text = modlog.render(
        chat_id=CHAT_ID, action="بن", target=1, actor=7, actor_source="admin"
    )

    assert "<code>7</code> (ادمین)" in text


def test_a_known_admin_id_wins_over_a_missing_source() -> None:
    text = modlog.render(chat_id=CHAT_ID, action="بن", target=1, actor=7)

    assert "<code>7</code>" in text
    assert "ادمین" not in text


def test_an_unknown_source_never_falls_back_to_a_bot_label() -> None:
    text = modlog.render(
        chat_id=CHAT_ID, action="بن", target=1, actor=None, actor_source="alien"
    )

    assert "سیستم/ربات" in text
    assert "Master" not in text and "Watchdog" not in text


# -------------------------------------------------------- content report -----
def test_the_report_shows_the_numeric_id_and_the_action() -> None:
    text = modlog.render_report(
        group_title="My <Group>",
        target=42,
        action="سکوت دائم (Permanent Mute)",
        reason="ارسال محتوای مستهجن (Pornographic Media)",
        target_name="Ali & Co",
    )

    assert "🚨 گزارش اعمال قانون (تخلف محتوا)" in text
    assert "👥 گروه: My &lt;Group&gt;" in text
    assert '<a href="tg://user?id=42">Ali &amp; Co</a>' in text
    assert "🆔 آیدی عددی: <code>42</code>" in text
    assert "⚖️ اقدام ربات: سکوت دائم (Permanent Mute)" in text
    assert "📝 علت: ارسال محتوای مستهجن (Pornographic Media)" in text


def test_the_report_links_the_offender_even_without_a_name() -> None:
    text = modlog.render_report(
        group_title="G", target=42, action="سکوت دائم (Permanent Mute)", reason="r"
    )

    assert '<a href="tg://user?id=42">42</a>' in text


def test_the_report_reaches_the_owner_and_the_subadmin(monkeypatch) -> None:
    _install_roster(monkeypatch, [7], [9])
    bot = FakeBot()

    sent = asyncio.run(
        modlog.deliver(
            bot,
            CHAT_ID,
            action="سکوت دائم (Permanent Mute)",
            target=42,
            reason="ارسال محتوای مستهجن (Pornographic Media)",
            template="content",
            target_name="Ali",
        )
    )

    assert sent == 2
    assert [row[0] for row in bot.sent] == [7, 9]
    assert "🆔 آیدی عددی: <code>42</code>" in bot.sent[0][1]
    assert bot.sent[0][2] == "HTML"


# -------------------------------------------------------------- deliver ------
def test_delivery_reaches_every_assigned_admin(monkeypatch) -> None:
    _install_roster(monkeypatch, [7, 9], [11])
    bot = FakeBot()

    sent = asyncio.run(
        modlog.deliver(bot, CHAT_ID, action="بن", target=42, actor=7)
    )

    assert sent == 3
    assert [row[0] for row in bot.sent] == [7, 9, 11]
    assert bot.sent[0][1] == bot.sent[2][1], "every admin gets the same record"
    assert bot.sent[0][2] == "HTML"


def test_a_blocked_admin_never_aborts_the_fan_out(monkeypatch) -> None:
    _install_roster(monkeypatch, [7, 9], [])
    bot = FakeBot(fail={7: TelegramForbiddenError(method=None, message="blocked")})

    sent = asyncio.run(modlog.deliver(bot, CHAT_ID, action="بن", target=42))

    assert sent == 1
    assert [row[0] for row in bot.sent] == [9]


def test_a_bad_request_counts_as_undeliverable(monkeypatch) -> None:
    _install_roster(monkeypatch, [7, 9], [])
    bot = FakeBot(fail={7: TelegramBadRequest(method=None, message="chat not found")})

    assert asyncio.run(modlog.deliver(bot, CHAT_ID, action="بن", target=42)) == 1


def test_a_flood_wait_is_retried_once(monkeypatch) -> None:
    _install_roster(monkeypatch, [7], [])

    assert (
        asyncio.run(
            modlog.deliver(FakeBot(flood_once=True), CHAT_ID, action="بن", target=42)
        )
        == 1
    )


def test_a_total_transport_failure_never_raises(monkeypatch) -> None:
    _install_roster(monkeypatch, [7, 9], [])

    bot = FakeBot(fail={7: RuntimeError("boom"), 9: RuntimeError("boom")})

    assert asyncio.run(modlog.deliver(bot, CHAT_ID, action="بن", target=42)) == 0
    assert bot.sent == []


def test_no_roster_or_no_bot_means_no_send(monkeypatch) -> None:
    _install_roster(monkeypatch, [], [])

    assert asyncio.run(modlog.deliver(FakeBot(), CHAT_ID, action="بن", target=42)) == 0
    assert asyncio.run(modlog.deliver(None, CHAT_ID, action="بن", target=42)) == 0


def test_an_explicit_roster_overrides_the_database(monkeypatch) -> None:
    def _fail(chat_id):  # pragma: no cover - must never run
        raise AssertionError("the database must not be touched")

    async def _boom(chat_id):
        return _fail(chat_id)

    monkeypatch.setattr(rbac, "list_admins", _boom)
    monkeypatch.setattr(rbac, "list_subadmins", _boom)
    bot = FakeBot()

    sent = asyncio.run(
        modlog.deliver(bot, CHAT_ID, action="بن", target=42, roster=[7])
    )

    assert sent == 1
    assert [row[0] for row in bot.sent] == [7]


# ---------------------------------------------------------------- notify -----
def test_notify_is_scheduled_and_never_blocks_the_caller(monkeypatch) -> None:
    ran: list[dict] = []

    async def _deliver(bot, chat_id, **kwargs):
        ran.append({"chat_id": chat_id, **kwargs})
        return 1

    monkeypatch.setattr(modlog, "deliver", _deliver)

    async def _main() -> None:
        modlog.notify(FakeBot(), CHAT_ID, action="بن", target=42, actor=7)
        assert ran == [], "notify must return before the fan-out finishes"
        await asyncio.sleep(0)

    asyncio.run(_main())
    assert len(ran) == 1
    assert {k: ran[0][k] for k in ("chat_id", "action", "target", "actor")} == {
        "chat_id": CHAT_ID,
        "action": "بن",
        "target": 42,
        "actor": 7,
    }


def test_notify_without_a_running_loop_is_a_no_op() -> None:
    assert modlog.notify(None, CHAT_ID, action="بن", target=42) is None


def test_a_crashing_fan_out_is_swallowed(monkeypatch) -> None:
    async def _deliver(bot, chat_id, **kwargs):
        raise RuntimeError("transport died")

    monkeypatch.setattr(modlog, "deliver", _deliver)

    async def _main() -> None:
        modlog.notify(FakeBot(), CHAT_ID, action="بن", target=42)
        await asyncio.sleep(0.05)
        assert not modlog._tasks, "finished tasks must be dropped"

    asyncio.run(_main())  # must not raise


def test_end_to_end_private_delivery(monkeypatch) -> None:
    _install_roster(monkeypatch, [7], [9])
    bot = FakeBot()

    async def _main() -> None:
        modlog.notify(bot, CHAT_ID, action="بن", target=42, actor=7, reason="پورن")
        for _ in range(200):
            if bot.sent and not modlog._tasks:
                break
            await asyncio.sleep(0.01)
        assert bot.sent, "the card must reach the admins"
        assert not modlog._tasks, "the fan-out must finish, not leak"

    asyncio.run(_main())
    assert sorted(row[0] for row in bot.sent) == [7, 9]
    assert "پورن" in bot.sent[0][1]


# ----------------------------------------------------------- escalation -----
def test_the_escalation_card_carries_everything_the_owner_needs() -> None:
    text = modlog.render_escalation(
        group_title="G & Co",
        target=42,
        reason="کلمه رکیک (bad)",
        target_name="Ali <X>",
    )

    assert text.startswith("⚠️ هشدار نیاز به بررسی ادمین سطح ۲ (مالک)")
    assert "👥 گروه: G &amp; Co" in text
    assert '<a href="tg://user?id=42">Ali &lt;X&gt;</a>' in text
    assert "🆔 آیدی عددی: <code>42</code>" in text
    assert "⚖️ وضعیت فعلی: سکوت دائم توسط ربات" in text
    assert (
        "📝 علت تخلف: کلمه رکیک (bad) (رسیدن به سقف اخطار / تخلف بحرانی)" in text
    )
    assert "دستور «بن»" in text


def test_the_escalation_links_the_offender_even_without_a_name() -> None:
    text = modlog.render_escalation(group_title="G", target=42, reason="r")

    assert '<a href="tg://user?id=42">42</a>' in text


def test_the_escalation_is_strictly_the_level_2_owner(monkeypatch) -> None:
    _install_roster(monkeypatch, [7], [9, 11])
    bot = FakeBot()

    sent = asyncio.run(
        modlog.deliver_escalation(bot, CHAT_ID, target=42, reason="اخطار دستی (2)")
    )

    assert sent == 1
    assert [row[0] for row in bot.sent] == [7], "Level 1 must never get the alert"
    assert bot.sent[0][2] == "HTML"


def test_no_owner_means_no_alert(monkeypatch) -> None:
    _install_roster(monkeypatch, [], [9])
    bot = FakeBot()

    sent = asyncio.run(
        modlog.deliver_escalation(bot, CHAT_ID, target=42, reason="r")
    )

    assert sent == 0
    assert bot.sent == []


def test_escalate_schedules_without_blocking_the_caller(monkeypatch) -> None:
    ran: list[dict] = []

    async def _deliver(bot, chat_id, **kwargs):
        ran.append({"chat_id": chat_id, **kwargs})
        return 1

    monkeypatch.setattr(modlog, "deliver_escalation", _deliver)

    async def _main() -> None:
        modlog.escalate(FakeBot(), CHAT_ID, target=42, reason="r")
        assert ran == [], "escalate must return before the alert lands"
        await asyncio.sleep(0)

    asyncio.run(_main())
    assert len(ran) == 1
    assert ran[0]["chat_id"] == CHAT_ID and ran[0]["target"] == 42


def test_escalate_without_a_running_loop_is_a_no_op() -> None:
    assert modlog.escalate(None, CHAT_ID, target=42, reason="r") is None
