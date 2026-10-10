from __future__ import annotations

EXPECTED_MASTER_ORDER = [
    "master.help",  # /help + help:* callbacks claim the update first
    "master.pv-fsm",
    "master.pv",
    "master.rbac",
    "master.modcmd",  # before the panel FSM so a command is never eaten as input
    "master.panel-fsm",
    "master.panel",
    "master.guards",
    "master.members",
]


def _names(dp) -> list[str]:
    return [router.name for router in dp.sub_routers]


def test_master_router_order(master) -> None:
    _bot, dp = master
    assert _names(dp) == EXPECTED_MASTER_ORDER


def test_help_is_mounted_before_the_command_and_guard_routers(master) -> None:
    _bot, dp = master
    names = _names(dp)
    assert names.index("master.help") < names.index("master.rbac")
    assert names.index("master.help") < names.index("master.guards")
    assert names.index("master.modcmd") < names.index("master.panel-fsm")
    assert names.index("master.modcmd") < names.index("master.guards")


def test_worker_stays_media_only(worker) -> None:
    _bot, dp = worker
    assert _names(dp) == ["worker.media"]
