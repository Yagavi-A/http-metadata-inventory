"""Aggregate router for the v1 API."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import health, metadata

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(metadata.router)
