"""Service-layer tests that exercise the repository + fetcher together."""

from __future__ import annotations

import asyncio

import pytest

from app.models.schemas import MetadataStatus
from app.services.fetcher import FetchError, FetchResult
from app.services.metadata_service import MetadataService
from app.services.url_security import URLSecurityPolicy

pytestmark = pytest.mark.asyncio

_PERMISSIVE = URLSecurityPolicy(block_private_networks=False)


@pytest.fixture
def service(repository, fake_fetcher) -> MetadataService:
    return MetadataService(
        repository=repository,
        fetcher=fake_fetcher,  # type: ignore[arg-type]
        security_policy=_PERMISSIVE,
    )


async def test_create_stores_completed_record(service, fake_fetcher) -> None:
    """``create`` runs the fetcher synchronously and persists a completed record."""
    fake_fetcher.set_result(
        "https://example.com/",
        FetchResult(
            http_status=200,
            headers={"x": "y"},
            cookies=[{"name": "a", "value": "1", "domain": "example.com", "path": "/"}],
            page_source="<p>hello</p>",
            final_url="https://example.com/",
        ),
    )

    record = await service.create("https://example.com")

    assert record.status is MetadataStatus.COMPLETED
    assert record.normalized_url == "https://example.com/"
    assert record.page_source == "<p>hello</p>"
    assert record.headers == {"x": "y"}


async def test_create_persists_failure_and_reraises(service, fake_fetcher, repository) -> None:
    """A fetch error is stored as a failed record *and* re-raised to the caller."""
    fake_fetcher.set_error("https://example.com/", FetchError("boom"))

    with pytest.raises(FetchError):
        await service.create("https://example.com")

    stored = await repository.get("https://example.com/")
    assert stored is not None
    assert stored.status is MetadataStatus.FAILED
    assert stored.error == "boom"


async def test_get_or_schedule_hits_cache(service, fake_fetcher) -> None:
    """``get_or_schedule`` returns the cached completed record without re-fetching."""
    fake_fetcher.set_result(
        "https://example.com/",
        FetchResult(http_status=200, headers={}, cookies=[], page_source="ok"),
    )
    await service.create("https://example.com")

    outcome = await service.get_or_schedule("https://example.com")

    assert outcome.is_hit is True
    assert outcome.scheduled is False
    assert outcome.record is not None
    assert outcome.record.status is MetadataStatus.COMPLETED


async def test_get_or_schedule_creates_pending_on_miss(service) -> None:
    """On a cache miss, ``get_or_schedule`` creates a pending record and reports it was scheduled."""
    outcome = await service.get_or_schedule("https://example.com")

    assert outcome.scheduled is True
    assert outcome.record is not None
    assert outcome.record.status is MetadataStatus.PENDING


async def test_get_or_schedule_second_call_does_not_reschedule(service) -> None:
    """A repeat call while the record is still pending does not re-enqueue another job."""
    first = await service.get_or_schedule("https://example.com")
    second = await service.get_or_schedule("https://example.com")

    assert first.scheduled is True
    assert second.scheduled is False
    assert second.record is not None
    assert second.record.status is MetadataStatus.PENDING


async def test_get_or_schedule_with_max_age_returns_fresh_hit(service, fake_fetcher) -> None:
    """A completed record younger than ``max_age`` is served as a cache hit."""
    fake_fetcher.set_result(
        "https://example.com/",
        FetchResult(http_status=200, headers={}, cookies=[], page_source="ok"),
    )
    await service.create("https://example.com")

    outcome = await service.get_or_schedule("https://example.com", max_age=3600)

    assert outcome.is_hit is True
    assert outcome.scheduled is False
    assert outcome.record is not None
    assert outcome.record.status is MetadataStatus.COMPLETED


async def test_get_or_schedule_with_max_age_schedules_refresh_when_stale(
    service, fake_fetcher
) -> None:
    """A completed record older than ``max_age`` is flipped to pending and rescheduled."""
    fake_fetcher.set_result(
        "https://example.com/",
        FetchResult(http_status=200, headers={}, cookies=[], page_source="old"),
    )
    await service.create("https://example.com")
    # ``max_age=0`` forces every completed record to look stale.
    await asyncio.sleep(0.01)

    outcome = await service.get_or_schedule("https://example.com", max_age=0)

    assert outcome.scheduled is True
    assert outcome.record is not None
    assert outcome.record.status is MetadataStatus.PENDING


async def test_get_or_schedule_with_max_age_ignores_failed_records(service, fake_fetcher) -> None:
    """A failed record is never re-scheduled by ``max_age`` (only completed ones are)."""
    fake_fetcher.set_error("https://example.com/", FetchError("boom"))
    with pytest.raises(FetchError):
        await service.create("https://example.com")

    outcome = await service.get_or_schedule("https://example.com", max_age=0)

    assert outcome.scheduled is False
    assert outcome.record is not None
    assert outcome.record.status is MetadataStatus.FAILED
