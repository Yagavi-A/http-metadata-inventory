"""Tests for the in-process background worker pool."""

from __future__ import annotations

import asyncio

import pytest

from app.workers.background import BackgroundWorker, FetchJob

pytestmark = pytest.mark.asyncio


async def test_worker_processes_jobs_concurrently() -> None:
    """All submitted jobs are eventually processed by the worker pool."""
    processed: list[str] = []

    async def handler(job: FetchJob) -> None:
        await asyncio.sleep(0.01)
        processed.append(job.normalized_url)

    worker = BackgroundWorker(handler=handler, concurrency=3, queue_maxsize=10)
    await worker.start()
    try:
        for i in range(5):
            await worker.submit(
                FetchJob(normalized_url=f"https://example.com/{i}", original_url=f"u{i}")
            )
        await worker.join()
    finally:
        await worker.stop()

    assert sorted(processed) == [f"https://example.com/{i}" for i in range(5)]


async def test_worker_dedupes_same_url_while_in_flight() -> None:
    """A duplicate submission for an in-flight URL is dropped (single fetch only)."""
    in_flight = asyncio.Event()
    proceed = asyncio.Event()
    seen: list[str] = []

    async def handler(job: FetchJob) -> None:
        seen.append(job.normalized_url)
        in_flight.set()
        await proceed.wait()

    worker = BackgroundWorker(handler=handler, concurrency=1, queue_maxsize=10)
    await worker.start()
    try:
        first = await worker.submit(
            FetchJob(normalized_url="https://example.com/", original_url="u")
        )
        await in_flight.wait()
        duplicate = await worker.submit(
            FetchJob(normalized_url="https://example.com/", original_url="u")
        )
        proceed.set()
        await worker.join()
    finally:
        await worker.stop()

    assert first is True
    assert duplicate is False
    assert seen == ["https://example.com/"]


async def test_worker_swallows_handler_exceptions() -> None:
    """A handler raising on one job does not crash the worker; subsequent jobs still run."""
    calls: list[str] = []

    async def handler(job: FetchJob) -> None:
        calls.append(job.normalized_url)
        raise RuntimeError("boom")

    worker = BackgroundWorker(handler=handler, concurrency=1, queue_maxsize=10)
    await worker.start()
    try:
        await worker.submit(FetchJob(normalized_url="https://example.com/a", original_url="a"))
        await worker.submit(FetchJob(normalized_url="https://example.com/b", original_url="b"))
        await worker.join()
    finally:
        await worker.stop()

    assert sorted(calls) == ["https://example.com/a", "https://example.com/b"]
