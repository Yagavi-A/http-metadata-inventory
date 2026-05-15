"""Per-domain concurrency throttling for outbound fetches.

The :class:`RateLimiter` protocol lets us keep :class:`HTTPFetcher`
ignorant of *how* throttling is implemented.  In-process callers can
plug in :class:`PerDomainSemaphoreLimiter`; production deployments could
swap in a token-bucket or Redis-backed limiter without touching the
fetcher.

This is a deliberate hook, not a hardened rate-limiter: it caps
**concurrent** requests per host, not per-second throughput.  For a
real crawler you would also want:

    * a leaky-bucket rate (req/sec per host),
    * polite ``robots.txt`` handling,
    * shared state across worker processes (e.g. Redis-backed).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Protocol


class RateLimiter(Protocol):
    """Async context manager keyed by host name."""

    def limit(self, host: str):  # pragma: no cover - protocol
        """Return an async context manager that gates entry for *host*."""
        ...


class NoOpRateLimiter:
    """Default limiter that performs no throttling (production default)."""

    @asynccontextmanager
    async def limit(self, host: str):
        yield


class PerDomainSemaphoreLimiter:
    """Cap the number of in-flight fetches per host with an ``asyncio.Semaphore``.

    The semaphore is allocated lazily on first use of a host name and
    kept for the process lifetime.  ``max_concurrent`` must be >= 1.
    """

    def __init__(self, max_concurrent: int) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self._max = max_concurrent
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._lock = asyncio.Lock()

    async def _semaphore_for(self, host: str) -> asyncio.Semaphore:
        async with self._lock:
            sem = self._semaphores.get(host)
            if sem is None:
                sem = asyncio.Semaphore(self._max)
                self._semaphores[host] = sem
            return sem

    @asynccontextmanager
    async def limit(self, host: str):
        sem = await self._semaphore_for(host)
        async with sem:
            yield


def build_rate_limiter(max_concurrent_per_domain: int) -> RateLimiter:
    """Factory that returns the appropriate limiter for the configured cap.

    A value of ``0`` means "no throttling" and yields the cheap no-op
    implementation.
    """
    if max_concurrent_per_domain <= 0:
        return NoOpRateLimiter()
    return PerDomainSemaphoreLimiter(max_concurrent_per_domain)
