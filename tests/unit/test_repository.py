"""Repository unit tests against an in-memory Mongo."""

from __future__ import annotations

import pytest

from app.models.schemas import MetadataStatus
from app.storage.metadata import MetadataRepository

pytestmark = pytest.mark.asyncio


async def test_insert_pending_returns_record_on_first_call(
    repository: MetadataRepository,
) -> None:
    """First ``insert_pending`` for a URL returns a freshly-created pending record."""
    record = await repository.insert_pending("https://example.com/", "https://example.com")
    assert record is not None
    assert record.normalized_url == "https://example.com/"
    assert record.status is MetadataStatus.PENDING
    assert record.attempts == 0


async def test_insert_pending_is_idempotent(
    repository: MetadataRepository,
) -> None:
    """Second ``insert_pending`` for the same URL returns ``None`` (no duplicate scheduling)."""
    first = await repository.insert_pending("https://example.com/", "https://example.com")
    second = await repository.insert_pending("https://example.com/", "https://example.com")
    assert first is not None
    assert second is None  # already exists


async def test_save_success_replaces_pending(
    repository: MetadataRepository,
) -> None:
    """``save_success`` upgrades a pending record to completed with the fetched payload."""
    await repository.insert_pending("https://example.com/", "https://example.com")
    saved = await repository.save_success(
        normalized_url="https://example.com/",
        original_url="https://example.com",
        http_status=200,
        headers={"content-type": "text/html"},
        cookies=[{"name": "k", "value": "v", "domain": "example.com", "path": "/"}],
        page_source="<html></html>",
    )
    assert saved.status is MetadataStatus.COMPLETED
    assert saved.http_status == 200
    assert saved.headers["content-type"] == "text/html"
    assert saved.cookies[0].name == "k"
    assert saved.page_source == "<html></html>"
    assert saved.attempts == 1


async def test_save_failure_records_error(
    repository: MetadataRepository,
) -> None:
    """``save_failure`` persists the error message and flips the record to ``failed``."""
    saved = await repository.save_failure(
        normalized_url="https://example.com/",
        original_url="https://example.com",
        error="boom",
    )
    assert saved.status is MetadataStatus.FAILED
    assert saved.error == "boom"


async def test_get_returns_none_for_unknown_url(
    repository: MetadataRepository,
) -> None:
    """``get`` returns ``None`` for a URL that has never been stored."""
    assert await repository.get("https://nope.example.com/") is None


async def test_list_pending_returns_only_pending_records(
    repository: MetadataRepository,
) -> None:
    """``list_pending`` filters by status so completed records are not re-enqueued."""
    await repository.insert_pending("https://a.example/", "https://a.example")
    await repository.insert_pending("https://b.example/", "https://b.example")
    await repository.save_success(
        normalized_url="https://b.example/",
        original_url="https://b.example",
        http_status=200,
        headers={},
        cookies=[],
        page_source="",
    )

    pending = await repository.list_pending()

    urls = sorted(r.normalized_url for r in pending)
    assert urls == ["https://a.example/"]


async def test_mark_refresh_pending_flips_completed_back(
    repository: MetadataRepository,
) -> None:
    """``mark_refresh_pending`` flips a completed record back to pending while keeping its body."""
    await repository.save_success(
        normalized_url="https://example.com/",
        original_url="https://example.com",
        http_status=200,
        headers={"x": "y"},
        cookies=[],
        page_source="old",
    )

    refreshed = await repository.mark_refresh_pending("https://example.com/", "https://example.com")

    assert refreshed is not None
    assert refreshed.status is MetadataStatus.PENDING
    # Existing payload should still be there so the next refresh can reuse it.
    assert refreshed.page_source == "old"


async def test_mark_refresh_pending_returns_none_when_missing(
    repository: MetadataRepository,
) -> None:
    """Refreshing a URL that does not exist is a no-op."""
    result = await repository.mark_refresh_pending(
        "https://nope.example.com/", "https://nope.example.com"
    )
    assert result is None
