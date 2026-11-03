from __future__ import annotations

import logging
from dataclasses import dataclass, field

import aiohttp

from abgguard.config import settings

log = logging.getLogger("abgguard.antiporn")

_TIMEOUT = aiohttp.ClientTimeout(
    total=settings.porn_api_timeout,
    connect=5.0,
    sock_read=settings.porn_api_timeout,
)


@dataclass(slots=True)
class PornVerdict:
    is_porn: bool = False
    score: float = 0.0
    labels: list[str] = field(default_factory=list)
    available: bool = True

    @property
    def summary(self) -> str:
        if not self.available:
            return "microservice offline"
        return f"{self.score:.2f} {','.join(self.labels) or '-'}"


class AntiPornClient:
    """
    Talks to the NudeNet / YOLO microservice that runs on the *same* host.
    No TLS, no public DNS - plain loopback HTTP so latency stays < 5 ms.
    """

    def __init__(self, url: str | None = None, threshold: float | None = None) -> None:
        self.url = url or settings.porn_api_url
        self.threshold = threshold if threshold is not None else settings.porn_threshold
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=_TIMEOUT)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def health(self) -> bool:
        try:
            session = await self._get_session()
            base = self.url.rsplit("/", 1)[0]
            async with session.get(f"{base}/health") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def scan_bytes(self, payload: bytes, content_type: str = "image/jpeg") -> PornVerdict:
        if not payload:
            return PornVerdict(available=False)

        form = aiohttp.FormData()
        form.add_field(
            "file",
            payload,
            filename="frame.bin",
            content_type=content_type,
        )
        form.add_field("threshold", str(self.threshold))

        try:
            session = await self._get_session()
            async with session.post(self.url, data=form) as resp:
                if resp.status != 200:
                    log.warning("porn api http %s", resp.status)
                    return PornVerdict(available=False)
                data = await resp.json(content_type=None)
        except Exception as exc:
            # fail-open: a dead detector must never freeze the worker
            log.warning("porn api unreachable: %s: %s", type(exc).__name__, exc)
            return PornVerdict(available=False)

        score = float(data.get("score", 0.0))
        labels = [str(x) for x in data.get("labels", [])]
        detected = bool(data.get("is_porn")) or score >= self.threshold
        return PornVerdict(is_porn=detected, score=score, labels=labels, available=True)


porn_client = AntiPornClient()
