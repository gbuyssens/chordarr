"""Shared HTTP client with a per-host token bucket, retries and a friendly User-Agent."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from chordarr.config import settings

log = logging.getLogger(__name__)


class RateLimiter:
    """Allow at most ``rate`` requests per second, spaced evenly."""

    def __init__(self, rate: float):
        self.interval = 1.0 / rate
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now < self._next:
                await asyncio.sleep(self._next - now)
                now = time.monotonic()
            self._next = now + self.interval


class Client:
    def __init__(self, base_url: str, rate: float = 10, timeout: float = 30):
        self.base_url = base_url.rstrip("/")
        self.limiter = RateLimiter(rate)
        self._client = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
            timeout=timeout,
            follow_redirects=True,
        )

    async def get_json(self, path: str, params: dict | None = None, retries: int = 4) -> dict | list:
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        delay = 1.0
        for attempt in range(retries + 1):
            await self.limiter.wait()
            try:
                r = await self._client.get(url, params=params)
            except httpx.TransportError as exc:
                if attempt == retries:
                    raise
                log.warning("%s: transport error %s, retrying", url, exc)
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if r.status_code in (429, 503) and attempt < retries:
                retry_after = r.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
                log.warning("%s: HTTP %s, waiting %.1fs", url, r.status_code, wait)
                await asyncio.sleep(wait)
                delay *= 2
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("unreachable")

    async def aclose(self) -> None:
        await self._client.aclose()
