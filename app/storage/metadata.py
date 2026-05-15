"""Storage layer for metadata documents.

This module is the only place that knows about Mongo and its document
shape.  Everything else in the codebase deals with :class:`MetadataRecord`.

The class is still called ``MetadataRepository`` because *repository* is
the well-known data-access pattern name; only the package was renamed
from ``repositories`` to ``storage`` for clarity.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo import ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.models.schemas import (
    MetadataRecord,
    MetadataStatus,
    doc_to_record,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MetadataRepository:
    """Async repository over a single Mongo collection."""

    def __init__(self, collection: AsyncIOMotorCollection) -> None:
        self._collection = collection

    async def ensure_indexes(self) -> None:
        """Create supporting indexes.  ``_id`` is unique by construction."""
        await self._collection.create_index(
            [("status", ASCENDING), ("updated_at", ASCENDING)],
            name="status_updated_at_idx",
        )

    async def get(self, normalized_url: str) -> MetadataRecord | None:
        """Return the record for *normalized_url*, or ``None`` if absent."""
        doc = await self._collection.find_one({"_id": normalized_url})
        if doc is None:
            return None
        return doc_to_record(doc)

    async def insert_pending(self, normalized_url: str, original_url: str) -> MetadataRecord | None:
        """Create a placeholder ``pending`` document.

        Returns the created record, or ``None`` if a document with the same
        ``_id`` already exists.  Callers use that signal to enqueue a
        background job exactly once per URL.
        """
        now = _utcnow()
        doc = {
            "_id": normalized_url,
            "url": original_url,
            "status": MetadataStatus.PENDING.value,
            "http_status": None,
            "headers": {},
            "cookies": [],
            "page_source": None,
            "error": None,
            "attempts": 0,
            "created_at": now,
            "updated_at": now,
            "fetched_at": None,
        }
        try:
            await self._collection.insert_one(doc)
        except DuplicateKeyError:
            return None
        return doc_to_record(doc)

    async def mark_refresh_pending(
        self, normalized_url: str, original_url: str
    ) -> MetadataRecord | None:
        """Flip an existing completed record back to ``pending`` for re-collection.

        Used by the ``?max_age=`` path so a stale cached entry gets a new
        fetch attempt.  Returns the updated record or ``None`` if no
        document existed.
        """
        now = _utcnow()
        update = {
            "$set": {
                "url": original_url,
                "status": MetadataStatus.PENDING.value,
                "error": None,
                "updated_at": now,
            },
        }
        doc = await self._collection.find_one_and_update(
            {"_id": normalized_url},
            update,
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            return None
        return doc_to_record(doc)

    async def list_pending(self, *, limit: int = 1000) -> list[MetadataRecord]:
        """Return every record currently in the ``pending`` state.

        Called once at startup so jobs that were queued in a previous
        process incarnation get re-enqueued in this one.
        """
        cursor = self._collection.find({"status": MetadataStatus.PENDING.value}).limit(limit)
        return [doc_to_record(doc) async for doc in cursor]

    async def save_success(
        self,
        *,
        normalized_url: str,
        original_url: str,
        http_status: int,
        headers: dict[str, str],
        cookies: list[dict[str, str | None]],
        page_source: str,
    ) -> MetadataRecord:
        """Upsert a ``completed`` record with the freshly-fetched payload."""
        now = _utcnow()
        update = {
            "$set": {
                "url": original_url,
                "status": MetadataStatus.COMPLETED.value,
                "http_status": http_status,
                "headers": headers,
                "cookies": cookies,
                "page_source": page_source,
                "error": None,
                "updated_at": now,
                "fetched_at": now,
            },
            "$setOnInsert": {"created_at": now},
            "$inc": {"attempts": 1},
        }
        doc = await self._collection.find_one_and_update(
            {"_id": normalized_url},
            update,
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            doc = await self._collection.find_one({"_id": normalized_url})
        return doc_to_record(doc)

    async def save_failure(
        self,
        *,
        normalized_url: str,
        original_url: str,
        error: str,
    ) -> MetadataRecord:
        """Upsert a ``failed`` record carrying the upstream error message."""
        now = _utcnow()
        update = {
            "$set": {
                "url": original_url,
                "status": MetadataStatus.FAILED.value,
                "error": error,
                "updated_at": now,
                "fetched_at": now,
            },
            "$setOnInsert": {"created_at": now},
            "$inc": {"attempts": 1},
        }
        doc = await self._collection.find_one_and_update(
            {"_id": normalized_url},
            update,
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            doc = await self._collection.find_one({"_id": normalized_url})
        return doc_to_record(doc)
