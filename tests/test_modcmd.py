from __future__ import annotations

from abgguard.master.handlers import modcmd


def test_slash_aliases_resolve_to_one_canonical_action() -> None:
    cases = {
        "/mute": "mute",
        "/MUTE 30": "mute",
        "/mute@abg_guard_bot 2h": "mute",
        "/سکوت": "mute",
        "/full_mute": "full_mute",
        "/fullmute": "full_mute",
        "/ban": "ban",
        "/بن": "ban",
        "/warn": "warn",
        "/اخطار 10": "warn",
        "/purge": "purge",
        "/purge 50": "purge",
        "/پاکسازی": "purge",
        "/unmute": "unmute",
        "/UNMUTE 99": "unmute",
    }
    for text, expected in cases.items():
        parsed = modcmd.parse_mod_command(text)
        assert parsed is not None, text
        assert parsed[0] == expected, text


def test_arguments_are_kept() -> None:
    assert modcmd.parse_mod_command("/mute 30") == ("mute", "30")
    assert modcmd.parse_mod_command("/purge 50") == ("purge", "50")
    assert modcmd.parse_mod_command("/mute  ") == ("mute", "")
    assert modcmd.parse_mod_command("/سکوت") == ("mute", "")


def test_unrelated_commands_and_sentences_are_ignored() -> None:
    for text in (None, "", "/start", "/panel", "/help@bot", "سکوت کن", "بن زدم", "hello"):
        assert modcmd.parse_mod_command(text) is None, text


def test_persian_triggers_only_match_as_a_whole_message() -> None:
    assert modcmd.is_plain_trigger("سکوت")
    assert modcmd.is_plain_trigger("  اخطار  ")
    assert not modcmd.is_plain_trigger("/سکوت")
    assert not modcmd.is_plain_trigger("سکوت کن")
    assert not modcmd.is_plain_trigger("بن")
    assert not modcmd.is_plain_trigger(None)


def test_rule_six_triggers_are_supported() -> None:
    assert {"سکوت", "اخطار"} == modcmd.PLAIN_TRIGGERS


def test_the_unmute_bare_triggers_are_supported() -> None:
    assert {"لغو سکوت", "حذف سکوت"} == modcmd.UNMUTE_TRIGGERS
    assert modcmd.is_plain_trigger("لغو سکوت")
    assert modcmd.is_plain_trigger("  حذف سکوت  ")
    assert not modcmd.is_plain_trigger("/لغو سکوت")

    for text in ("لغو سکوت", "  لغو سکوت  ", "حذف سکوت"):
        parsed = modcmd.parse_mod_command(text)
        assert parsed is not None, text
        assert parsed == ("unmute", ""), text


def test_parse_duration_defaults_and_units() -> None:
    assert modcmd.parse_duration("") == modcmd.DEFAULT_MUTE
    assert modcmd.parse_duration("nonsense") == modcmd.DEFAULT_MUTE
    assert modcmd.parse_duration("0") == modcmd.DEFAULT_MUTE
    assert modcmd.parse_duration("30") == 30 * 60
    assert modcmd.parse_duration("45s") == 45
    assert modcmd.parse_duration("2h") == 2 * 3600
    assert modcmd.parse_duration("1d") == 24 * 3600
    assert modcmd.parse_duration("30 99") == 30 * 60  # only the first token counts
    assert modcmd.parse_duration("999999d") == modcmd.MAX_MUTE


def test_parse_count_is_clamped() -> None:
    assert modcmd.parse_count("") == modcmd.DEFAULT_PURGE
    assert modcmd.parse_count("abc") == modcmd.DEFAULT_PURGE
    assert modcmd.parse_count("0") == 1
    assert modcmd.parse_count("7") == 7
    assert modcmd.parse_count("1000") == modcmd.MAX_PURGE


def test_resolve_target_prefers_the_replied_user() -> None:
    assert modcmd.resolve_target(42, "30") == (42, "30")
    assert modcmd.resolve_target(42, "12345 30") == (42, "12345 30")


def test_resolve_target_falls_back_to_a_raw_user_id() -> None:
    assert modcmd.resolve_target(None, "12345 30") == (12345, "30")
    assert modcmd.resolve_target(None, "12345") == (12345, "")
    assert modcmd.resolve_target(None, "not-a-id 30") == (None, "not-a-id 30")
    assert modcmd.resolve_target(None, "") == (None, "")


def test_humanize_reads_naturally_in_persian() -> None:
    assert modcmd.humanize(45) == "۴۵ ثانیه"
    assert modcmd.humanize(600) == "۱۰ دقیقه"
    assert modcmd.humanize(5400) == "۱ ساعت و ۳۰ دقیقه"
    assert modcmd.humanize(3600) == "۱ ساعت"
    assert modcmd.humanize(86400 * 2) == "۲ روز"
    assert modcmd.humanize(86400 * 2 + 3600 * 3) == "۲ روز و ۳ ساعت"


def test_stay_within_the_ban_log_reason_column() -> None:
    assert modcmd.REASON_LIMIT == 64
