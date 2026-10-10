from __future__ import annotations

import logging
from types import SimpleNamespace

from abgguard import config
from abgguard.core import rbac


def test_god_id_is_read_from_the_env_when_present(monkeypatch) -> None:
    monkeypatch.delenv("GOD_ADMIN_IDS", raising=False)
    monkeypatch.setenv("GOD_ADMIN_ID", "6029973681")
    assert config._god_admin_ids() == (6029973681,)


def test_god_id_falls_back_when_the_env_is_missing(monkeypatch, caplog) -> None:
    caplog.set_level(logging.ERROR)
    monkeypatch.delenv("GOD_ADMIN_IDS", raising=False)
    monkeypatch.delenv("GOD_ADMIN_ID", raising=False)

    assert config._god_admin_ids() == (config.FALLBACK_GOD_ADMIN_ID,)
    assert config.FALLBACK_GOD_ADMIN_ID == 6029973681
    assert any("falling back" in r.message for r in caplog.records)


def test_god_id_falls_back_when_the_env_is_unparsable(monkeypatch) -> None:
    monkeypatch.delenv("GOD_ADMIN_IDS", raising=False)
    monkeypatch.setenv("GOD_ADMIN_ID", "not-a-number")

    assert config._god_admin_ids() == (6029973681,)


def test_an_empty_env_cannot_revoke_the_fallback_god(monkeypatch) -> None:
    monkeypatch.setattr(rbac, "settings", SimpleNamespace(god_admin_ids=()))

    assert rbac.configured_gods() == {config.FALLBACK_GOD_ADMIN_ID}


def test_configured_gods_mirrors_the_env(monkeypatch) -> None:
    monkeypatch.setattr(rbac, "settings", SimpleNamespace(god_admin_ids=(1, 2)))

    assert rbac.configured_gods() == {1, 2}
