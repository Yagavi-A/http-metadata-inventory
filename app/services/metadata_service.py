"""High level orchestration for collecting and serving metadata.

This is the only layer that ties the repository and the fetcher together.
Both the API routes and the background worker call into this service so
that the business rules live in exactly one place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from app.models.schemas import MetadataRecord, MetadataStatus
from app.services.fetcher import FetchError, HTTPFetcher
from app.services.url_security import URLSecurityPolicy, assert_url_is_safe
from app.services.url_utils import normalize_url
from app.storage.metadata import MetadataRepository

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ScheduleOutcome:
    """Returned by :meth:`MetadataService.get_or_schedule`."""

    record: MetadataRecord | None
    scheduled: bool
    normalized_url: str
    original_url: str

    @property
    def is_hit(self) -> bool:
        """True when a cached record was served without scheduling new work."""
        return self.record is not None and not self.scheduled


class MetadataService:
    """Encapsulates the business rules around collecting and serving metadata."""

    def __init__(
        self,
        *,
        repository: MetadataRepository,
        fetcher: HTTPFetcher,
        security_policy: URLSecurityPolicy | None = None,
    ) -> None:
        self._repo = repository
        self._fetcher = fetcher
        self._policy = security_policy or URLSecurityPolicy()

    async def create(self, url: str) -> MetadataRecord:
        """Synchronously collect and store metadata for *url*.

        Used by the POST endpoint, which is documented as a "create"
        operation.  Errors are persisted as ``failed`` records and the
        exception is re-raised so the caller can decide how to surface it.
        """
        normalized = normalize_url(url)
        await assert_url_is_safe(normalized, self._policy)
        try:
            result = await self._fetcher.fetch(normalized)
        except FetchError as exc:
            logger.warning("Synchronous fetch failed for %s: %s", normalized, exc)
            await self._repo.save_failure(
                normalized_url=normalized,
                original_url=url,
                error=str(exc),
            )
            raise

        return await self._repo.save_success(
            normalized_url=normalized,
            original_url=url,
            http_status=result.http_status,
            headers=result.headers,
            cookies=result.cookies,
            page_source=result.page_source,
        )

    async def get_or_schedule(self, url: str, *, max_age: int | None = None) -> ScheduleOutcome:
        """Look the record up; if missing or stale, create a pending placeholder.

        When *max_age* is set, completed records older than that many
        seconds are treated as misses: the existing document is flipped
        back to ``pending`` and the caller is expected to push a fresh
        job onto the worker.
        """
        normalized = normalize_url(url)
        await assert_url_is_safe(normalized, self._policy)

        existing = await self._repo.get(normalized)

        if existing is not None and self._is_stale(existing, max_age):
            refreshed = await self._repo.mark_refresh_pending(normalized, url)
            if refreshed is None:
                # Lost a race; another caller already refreshed.
                current = await self._repo.get(normalized)
                return ScheduleOutcome(
                    record=current,
                    scheduled=False,
                    normalized_url=normalized,
                    original_url=url,
                )
            return ScheduleOutcome(
                record=refreshed,
                scheduled=True,
                normalized_url=normalized,
                original_url=url,
            )

        if existing is not None:
            return ScheduleOutcome(
                record=existing,
                scheduled=False,
                normalized_url=normalized,
                original_url=url,
            )

        pending = await self._repo.insert_pending(normalized, url)
        if pending is None:
            # Lost the race; another caller created the doc just now.
            current = await self._repo.get(normalized)
            return ScheduleOutcome(
                record=current,
                scheduled=False,
                normalized_url=normalized,
                original_url=url,
            )
        return ScheduleOutcome(
            record=pending,
            scheduled=True,
            normalized_url=normalized,
            original_url=url,
        )

    async def collect_in_background(
        self, *, normalized_url: str, original_url: str
    ) -> MetadataRecord:
        """Run by the background worker after a cache miss."""
        try:
            result = await self._fetcher.fetch(normalized_url)
        except FetchError as exc:
            logger.warning("Background fetch failed for %s: %s", normalized_url, exc)
            return await self._repo.save_failure(
                normalized_url=normalized_url,
                original_url=original_url,
                error=str(exc),
            )

        return await self._repo.save_success(
            normalized_url=normalized_url,
            original_url=original_url,
            http_status=result.http_status,
            headers=result.headers,
            cookies=result.cookies,
            page_source=result.page_source,
        )

    @staticmethod
    def _is_stale(record: MetadataRecord, max_age: int | None) -> bool:
        """True when *record* is completed and older than ``max_age`` seconds."""
        if max_age is None or record.status is not MetadataStatus.COMPLETED:
            return False
        updated_at = record.updated_at
        # Some Mongo drivers (mongomock) hand back naive datetimes; assume UTC.
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        age = (_utcnow() - updated_at).total_seconds()
        return age > max_age
