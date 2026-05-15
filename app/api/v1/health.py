"""Liveness/readiness probes."""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from app.db.mongo import get_database

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    mongo: str


@router.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """Lightweight check used by Docker's healthcheck."""
    try:
        await get_database().command("ping")
        mongo_status = "ok"
    except Exception:  # noqa: BLE001 - we never want this endpoint to 500
        mongo_status = "unavailable"
    overall = "ok" if mongo_status == "ok" else "degraded"
    return HealthResponse(status=overall, mongo=mongo_status)


@router.get(
    "/livez",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def livez() -> Response:
    """Process liveness; says nothing about dependencies."""
    return Response(status_code=status.HTTP_204_NO_CONTENT)
