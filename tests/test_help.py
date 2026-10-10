from __future__ import annotations

import re

from abgguard.core.rbac import Role
from abgguard.master.handlers import help as help_mod

PERSIAN = re.compile(r"[؀-ۿ]")


# --------------------------------------------------------------- menu model --
def test_every_menu_key_has_a_section() -> None:
    for role in Role:
        for key in help_mod.menu_keys(role):
            assert key in help_mod.SECTIONS, f"{role} -> unknown section {key!r}"


def test_menu_grows_with_the_permission_level() -> None:
    sizes = {role: len(help_mod.menu_keys(role)) for role in Role}
    assert sizes[Role.USER] == 1
    assert sizes[Role.SUBADMIN] == 5
    assert sizes[Role.ADMIN] == 7
    assert sizes[Role.GOD] == 10
    assert sizes[Role.USER] < sizes[Role.SUBADMIN] < sizes[Role.ADMIN] < sizes[Role.GOD]


def test_specified_sections_lead_each_menu() -> None:
    """Level 3 -> system/owners/harvester, 2 -> PV/locks/blacklist, 1 -> law."""
    assert help_mod.menu_keys(Role.GOD)[:3] == ("sys", "owners", "harvester")
    assert help_mod.menu_keys(Role.ADMIN)[:3] == ("pvdash", "blacklist", "subadmins")
    assert help_mod.menu_keys(Role.SUBADMIN)[0] == "law"
    assert help_mod.menu_keys(Role.USER) == ("about",)


def test_no_menu_shows_a_section_above_the_users_level() -> None:
    for role in Role:
        for section in help_mod.menu_for(role):
            assert section.min_role <= role, f"{role} sees {section.key}"


# -------------------------------------------------------------- pagination --
def test_page_count_rounds_up() -> None:
    assert help_mod.page_count(1) == 1
    assert help_mod.page_count(5) == 1
    assert help_mod.page_count(6) == 2
    assert help_mod.page_count(10) == 2


def test_page_slice_covers_everything_without_overlap() -> None:
    role = Role.ADMIN
    total = len(help_mod.menu_for(role))
    pages = help_mod.page_count(total)
    seen: list[str] = []
    for page in range(pages):
        seen += [s.key for s in help_mod.page_slice(role, page)]
    assert seen == [s.key for s in help_mod.menu_for(role)]
    # out-of-range pages clamp instead of raising
    assert help_mod.page_slice(role, 99) == help_mod.page_slice(role, pages - 1)
    assert help_mod.page_slice(role, -3) == help_mod.page_slice(role, 0)


# ------------------------------------------------------------- keyboards ----
def _texts(markup) -> list[str]:
    return [btn.text for row in markup.inline_keyboard for btn in row]


def _callbacks(markup) -> list[str]:
    return [
        btn.callback_data
        for row in markup.inline_keyboard
        for btn in row
        if btn.callback_data
    ]


def test_menu_kb_lists_the_current_page_only() -> None:
    role = Role.ADMIN
    markup = help_mod.menu_kb(role, 0)
    callbacks = _callbacks(markup)
    page_keys = [f"help:pg:{s.key}" for s in help_mod.page_slice(role, 0)]
    assert [c for c in callbacks if c.startswith("help:pg:")] == page_keys


def test_menu_kb_paginates_when_there_are_more_sections() -> None:
    first = help_mod.menu_kb(Role.ADMIN, 0)
    assert "▶️" in _texts(first)
    assert "◀️" not in _texts(first)

    last_page = help_mod.page_count(len(help_mod.menu_for(Role.ADMIN))) - 1
    last = help_mod.menu_kb(Role.ADMIN, last_page)
    assert "◀️" in _texts(last)
    assert "▶️" not in _texts(last)


def test_single_page_menu_has_no_nav_buttons() -> None:
    texts = _texts(help_mod.menu_kb(Role.USER, 0))
    assert "▶️" not in texts and "◀️" not in texts


def test_every_menu_has_back_and_close() -> None:
    for role in Role:
        markup = help_mod.menu_kb(role, 0)
        callbacks = _callbacks(markup)
        assert "pv:root" in callbacks, role
        assert "help:close" in callbacks, role
        assert help_mod.BACK_LABEL in _texts(markup)
        assert help_mod.CLOSE_LABEL in _texts(markup)


def test_every_sub_page_has_back_and_close() -> None:
    for section in help_mod.SECTIONS.values():
        markup = help_mod.page_kb(section)
        assert _callbacks(markup) == ["help:home", "help:close"]
        texts = _texts(markup)
        assert help_mod.BACK_LABEL in texts
        assert help_mod.CLOSE_LABEL in texts


def test_level_zero_gets_support_and_add_to_group_buttons() -> None:
    texts = _texts(help_mod.menu_kb(Role.USER, 0, bot_username="abg_guard_bot"))
    assert help_mod.ADDBOT_LABEL in texts
    assert help_mod.SUPPORT_LABEL in texts
    urls = [
        btn.url
        for row in help_mod.menu_kb(Role.USER, 0, "abg_guard_bot").inline_keyboard
        for btn in row
        if btn.url
    ]
    assert any(u.endswith("?startgroup=true") for u in urls)


def test_admin_menu_does_not_advertise_adding_the_bot() -> None:
    assert help_mod.ADDBOT_LABEL not in _texts(help_mod.menu_kb(Role.ADMIN, 0, "abg"))


def test_telegram_length_limits_are_respected() -> None:
    for role in Role:
        for text in _texts(help_mod.menu_kb(role, 0, "abg_guard_bot")):
            assert len(text) <= 64, text
    for section in help_mod.SECTIONS.values():
        assert len(section.button) <= 64, section.key
        assert len(f"help:pg:{section.key}") <= 64, section.key


# --------------------------------------------------------------- routing -----
def test_resolve_reads_every_known_callback() -> None:
    assert help_mod.resolve("help:home") == ("home", "")
    assert help_mod.resolve("help:close") == ("close", "")
    assert help_mod.resolve("help:idx:0") == ("idx", "0")
    assert help_mod.resolve("help:idx:12") == ("idx", "12")
    assert help_mod.resolve("help:pg:law") == ("pg", "law")
    assert help_mod.resolve("help:pg:god.owners") == ("", "")


def test_resolve_rejects_foreign_and_malformed_data() -> None:
    for data in (None, "", "pv:root", "help", "help:pg:nope", "help:idx:x", "help:what:1"):
        assert help_mod.resolve(data) == ("", ""), data


# ------------------------------------------------------------------ text -----
def test_menu_text_is_role_aware() -> None:
    assert "Level 0" in help_mod.menu_text(Role.USER)
    assert "Level 3" in help_mod.menu_text(Role.GOD)
    assert "گاد ادمین" in help_mod.menu_text(Role.GOD)
    assert "صفحهٔ" not in help_mod.menu_text(Role.USER)  # single page -> no pager


def test_menu_text_announces_paging_only_when_needed() -> None:
    assert "صفحهٔ ۱ از ۲" in help_mod.menu_text(Role.ADMIN, 0)
    assert "صفحهٔ ۲ از ۲" in help_mod.menu_text(Role.ADMIN, 1)
    assert "صفحهٔ" not in help_mod.menu_text(Role.SUBADMIN, 0)


def test_section_text_carries_title_and_body() -> None:
    text = help_mod.section_text(help_mod.SECTIONS["law"])
    assert text.startswith("⚖️ اعمال قانون")
    assert "/mute" in text and "/ban" in text and "/warn" in text and "/purge" in text
    assert "سکوت" in text and "بن" in text and "اخطار" in text and "پاکسازی" in text


def test_all_sections_are_fluent_persian() -> None:
    for key, section in help_mod.SECTIONS.items():
        assert PERSIAN.search(section.body), key
        assert len(section.body) > 120, key


def test_required_categories_exist() -> None:
    titles = {s.title for s in help_mod.SECTIONS.values()}
    assert "قفل‌ها و امنیت" in titles
    assert "اعمال قانون" in titles
    assert "تنظیمات گروه" in titles


# ------------------------------------------------------- clean chat protocol --
def test_deep_link_pattern_only_matches_the_help_payload() -> None:
    pattern = help_mod.GROUP_TRIGGER
    assert re.search(pattern, "/start help")
    assert re.search(pattern, "/start@abg_guard_bot help")
    assert re.search(pattern, "/start help ")
    assert re.search(pattern, "/start help extra") is None
    assert re.search(pattern, "/start") is None
    assert re.search(pattern, "/help") is None


def test_group_lifetime_is_ten_seconds() -> None:
    assert help_mod.DELETE_AFTER == 10.0


def test_the_group_guide_lives_sixty_seconds() -> None:
    assert help_mod.GUIDE_AFTER == 60.0
    assert help_mod.GUIDE_AFTER > help_mod.DELETE_AFTER


def test_the_group_guide_covers_every_requested_topic() -> None:
    guide = help_mod.guide_text()
    for topic in (
        "پنل", "/panel", "نام فراخوانی",  # how to open the panel
        "/اخطار", "/سکوت", "/بن", "/full_mute",  # mod commands
        "ویژه", "وی‌آی‌پی",  # the 3-hour VIP
        "ساب‌ادمین", "مالک گروه", "سطح ۱", "سطح ۲", "سطح ۳",  # permissions
        "پیوی",  # the permanent, interactive version
    ):
        assert topic in guide, topic


def test_the_group_guide_never_leaks_the_interactive_menu() -> None:
    guide = help_mod.guide_text()
    assert "Level" not in guide  # the flat guide speaks in سطح, not Level
    assert len(guide) < 4096  # a single Telegram message
    assert guide.count("<code>") == guide.count("</code>")
    assert guide.count("<b>") == guide.count("</b>")
