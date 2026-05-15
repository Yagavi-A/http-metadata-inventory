"""Pydantic v2 models used at the API boundary and the data layer."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class MetadataStatus(str, Enum):
    """Lifecycle states for a stored metadata document."""

    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class MetadataCreateRequest(BaseModel):
    url: HttpUrl


class CookieModel(BaseModel):
    """One cookie captured from the upstream response."""

    name: str
    value: str
    domain: str | None = None
    path: str | None = None


class MetadataRecord(BaseModel):
    """Internal representation of a metadata document."""

    model_config = ConfigDict(populate_by_name=True)

    normalized_url: str = Field(alias="_id")
    url: str
    status: MetadataStatus
    http_status: int | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    cookies: list[CookieModel] = Field(default_factory=list)
    page_source: str | None = None
    error: str | None = None
    attempts: int = 0
    created_at: datetime
    updated_at: datetime
    fetched_at: datetime | None = None


class MetadataResponse(BaseModel):
    """Shape returned by the API."""

    url: str
    normalized_url: str
    status: MetadataStatus
    http_status: int | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    cookies: list[CookieModel] = Field(default_factory=list)
    page_source: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    fetched_at: datetime | None = None

    @classmethod
    def from_record(cls, record: MetadataRecord) -> MetadataResponse:
        """Build the API response shape from an internal record."""
        return cls(
            url=record.url,
            normalized_url=record.normalized_url,
            status=record.status,
            http_status=record.http_status,
            headers=record.headers,
            cookies=record.cookies,
            page_source=record.page_source,
            error=record.error,
            created_at=record.created_at,
            updated_at=record.updated_at,
            fetched_at=record.fetched_at,
        )


class AcknowledgementResponse(BaseModel):
    """Returned with 202 when the collection has been scheduled."""

    url: str
    normalized_url: str
    status: MetadataStatus
    detail: str


class ErrorResponse(BaseModel):
    detail: str


def doc_to_record(doc: dict[str, Any]) -> MetadataRecord:
    """Translate a raw Mongo doc into a `MetadataRecord`."""
    return MetadataRecord.model_validate(doc)
