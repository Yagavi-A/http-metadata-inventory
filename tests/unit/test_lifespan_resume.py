"""Tests for the startup hook that re-enqueues stuck ``pending`` records."""

from __future__ import annotations

import pytest

from app.main import _resume_pending_jobs
from app.storage.metadata import MetadataRepository
from app.workers.background import BackgroundWorker, FetchJob

pytestmark = pytest.mark.asyncio


async def test_resume_pending_jobs_enqueues_existing_pending_records(
    repository: MetadataRepository,
) -> None:
    """Each ``pending`` doc from a previous run is submitted to the worker pool."""
    await repository.insert_pending("https://a.example/", "https://a.example")
    await repository.insert_pending("https://b.example/", "https://b.example")

    submitted: list[FetchJob] = []

    async def handler(job: FetchJob) -> None:
        submitted.append(job)

    worker = BackgroundWorker(handler=handler, concurrency=1, queue_maxsize=10)
    await worker.start()
    try:
        await _resume_pending_jobs(repository, worker)
        await worker.join()
    finally:
        await worker.stop()

    urls = sorted(job.normalized_url for job in submitted)
    assert urls == ["https://a.example/", "https://b.example/"]


async def test_resume_pending_jobs_is_noop_when_nothing_pending(
    repository: MetadataRepository,
) -> None:
    """With no pending docs, the resume helper exits without touching the worker."""
    submitted: list[FetchJob] = []

    async def handler(job: FetchJob) -> None:
        submitted.append(job)

    worker = BackgroundWorker(handler=handler, concurrency=1, queue_maxsize=10)
    await worker.start()
    try:
        await _resume_pending_jobs(repository, worker)
        await worker.join()
    finally:
        await worker.stop()

    assert submitted == []
