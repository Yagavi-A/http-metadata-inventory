"""Tests for the per-domain rate limiter hook."""

from __future__ import annotations

import asyncio

import pytest

from app.services.rate_limiter import (
    NoOpRateLimiter,
    PerDomainSemaphoreLimiter,
    build_rate_limiter,
)

# ``asyncio_mode = "auto"`` (pyproject.toml) auto-marks the async tests below.


async def test_no_op_limiter_does_not_block_anything() -> None:
    """The default ``NoOpRateLimiter`` never blocks, even on nested entry."""
    limiter = NoOpRateLimiter()
    async with limiter.limit("example.com"), limiter.limit("example.com"):
        assert True


async def test_per_domain_limiter_caps_concurrency() -> None:
    """``PerDomainSemaphoreLimiter`` enforces the configured cap for a single host."""
    limiter = PerDomainSemaphoreLimiter(max_concurrent=2)
    in_flight = 0
    peak = 0
    barrier = asyncio.Event()

    async def worker() -> None:
        nonlocal in_flight, peak
        async with limiter.limit("example.com"):
            in_flight += 1
            peak = max(peak, in_flight)
            await barrier.wait()
            in_flight -= 1

    tasks = [asyncio.create_task(worker()) for _ in range(5)]
    await asyncio.sleep(0.05)  # let the first two enter
    assert peak == 2  # the cap is enforced even though 5 tasks are racing
    barrier.set()
    await asyncio.gather(*tasks)


async def test_per_domain_limiter_isolates_hosts() -> None:
    """Limits are scoped per host, so traffic to host A does not block host B."""
    limiter = PerDomainSemaphoreLimiter(max_concurrent=1)
    proceed = asyncio.Event()
    entered: list[str] = []

    async def fetch(host: str) -> None:
        async with limiter.limit(host):
            entered.append(host)
            await proceed.wait()

    a = asyncio.create_task(fetch("a.example.com"))
    b = asyncio.create_task(fetch("b.example.com"))
    await asyncio.sleep(0.05)
    assert sorted(entered) == ["a.example.com", "b.example.com"]
    proceed.set()
    await asyncio.gather(a, b)


def test_build_rate_limiter_returns_noop_when_disabled() -> None:
    """``build_rate_limiter(0)`` short-circuits to the no-op limiter."""
    limiter = build_rate_limiter(0)
    assert isinstance(limiter, NoOpRateLimiter)


def test_build_rate_limiter_returns_semaphore_when_enabled() -> None:
    """``build_rate_limiter(N>0)`` returns the per-domain semaphore implementation."""
    limiter = build_rate_limiter(3)
    assert isinstance(limiter, PerDomainSemaphoreLimiter)


def test_per_domain_limiter_validates_argument() -> None:
    """``PerDomainSemaphoreLimiter`` rejects non-positive concurrency settings."""
    with pytest.raises(ValueError):
        PerDomainSemaphoreLimiter(max_concurrent=0)
