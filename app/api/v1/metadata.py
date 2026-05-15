"""Metadata REST endpoints.

Two routes:

* ``POST /v1/metadata`` performs a synchronous fetch and persists it.
* ``GET  /v1/metadata`` is cache-first: hits return 200; misses enqueue
  a background job and return 202.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse

from app.api.deps import get_background_worker, get_metadata_service
from app.models.schemas import (
    AcknowledgementResponse,
    ErrorResponse,
    MetadataCreateRequest,
    MetadataResponse,
    MetadataStatus,
)
from app.services.fetcher import FetchError
from app.services.metadata_service import MetadataService
from app.services.url_security import UnsafeURLError
from app.services.url_utils import InvalidURLError, normalize_url
from app.workers.background import BackgroundWorker, FetchJob

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/metadata", tags=["metadata"])


@router.post(
    "",
    response_model=MetadataResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid URL"},
        502: {"model": ErrorResponse, "description": "Upstream fetch failed"},
    },
    summary="Synchronously collect and store metadata for a URL",
)
async def create_metadata(
    payload: MetadataCreateRequest,
    service: Annotated[MetadataService, Depends(get_metadata_service)],
) -> MetadataResponse:
    """Fetch the URL upstream, persist the result, and return the full record."""
    url_str = str(payload.url)
    try:
        record = await service.create(url_str)
    except InvalidURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UnsafeURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FetchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return MetadataResponse.from_record(record)


@router.get(
    "",
    response_model=MetadataResponse,
    responses={
        202: {
            "model": AcknowledgementResponse,
            "description": "Collection scheduled in the background",
        },
        400: {"model": ErrorResponse, "description": "Invalid URL"},
    },
    summary="Look up cached metadata; schedule collection on miss",
)
async def read_metadata(
    service: Annotated[MetadataService, Depends(get_metadata_service)],
    worker: Annotated[BackgroundWorker, Depends(get_background_worker)],
    url: Annotated[str, Query(..., description="Absolute http(s) URL to look up")],
    max_age: Annotated[
        int | None,
        Query(
            ge=0,
            description=(
                "Maximum age in seconds. Completed records older than this "
                "are re-collected; the response is 202 while the refresh runs."
            ),
        ),
    ] = None,
):
    """Return the cached record (200) or schedule a background fetch (202)."""
    try:
        # Validate eagerly so we can return a clean 400 before touching Mongo.
        normalize_url(url)
        outcome = await service.get_or_schedule(url, max_age=max_age)
    except InvalidURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UnsafeURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    record = outcome.record

    # Cache hit on a completed record: return the full payload.
    if record is not None and record.status == MetadataStatus.COMPLETED:
        return MetadataResponse.from_record(record)

    # A pending placeholder exists (either freshly scheduled or in flight).
    if outcome.scheduled:
        await worker.submit(
            FetchJob(
                normalized_url=outcome.normalized_url,
                original_url=outcome.original_url,
            )
        )

    if record is not None and record.status == MetadataStatus.PENDING:
        ack = AcknowledgementResponse(
            url=record.url,
            normalized_url=record.normalized_url,
            status=record.status,
            detail="Metadata collection has been scheduled; retry the request shortly.",
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=ack.model_dump(mode="json"),
        )

    # A previous attempt failed: we still surface the record (200) so the
    # client can inspect ``error`` instead of seeing repeated 202s.
    if record is not None:
        return MetadataResponse.from_record(record)

    # Defensive fallback - should not happen in practice.
    ack = AcknowledgementResponse(
        url=outcome.original_url,
        normalized_url=outcome.normalized_url,
        status=MetadataStatus.PENDING,
        detail="Metadata collection has been scheduled; retry the request shortly.",
    )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=ack.model_dump(mode="json"),
    )
