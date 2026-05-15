"""MongoDB client lifecycle.

We use a thin module-level holder so that the same `AsyncIOMotorClient` is
reused across requests, and so tests can swap in a fake client (e.g.
`mongomock_motor.AsyncMongoMockClient`).
"""

from __future__ import annotations

import asyncio
import logging

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection, AsyncIOMotorDatabase

from app.core.config import Settings

logger = logging.getLogger(__name__)


class MongoState:
    """Module-level holder for the shared client + database handles."""

    client: AsyncIOMotorClient | None = None
    db: AsyncIOMotorDatabase | None = None


_state = MongoState()


def set_client(client: AsyncIOMotorClient, db_name: str) -> None:
    """Used by lifespan and by tests to inject a client."""
    _state.client = client
    _state.db = client[db_name]


def get_database() -> AsyncIOMotorDatabase:
    if _state.db is None:
        raise RuntimeError("Mongo database is not initialised")
    return _state.db


def get_collection(name: str) -> AsyncIOMotorCollection:
    return get_database()[name]


async def connect_with_retry(settings: Settings, max_attempts: int = 30) -> AsyncIOMotorClient:
    """Connect to Mongo with a simple retry loop to survive startup races."""
    delay = 1.0
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            client = AsyncIOMotorClient(
                settings.mongo_uri,
                serverSelectionTimeoutMS=2000,
                uuidRepresentation="standard",
            )
            await client.admin.command("ping")
            logger.info("Connected to MongoDB on attempt %d", attempt)
            return client
        except Exception as exc:  # noqa: BLE001 - we re-raise after exhausting attempts
            last_error = exc
            logger.warning("MongoDB not ready (attempt %d/%d): %s", attempt, max_attempts, exc)
            await asyncio.sleep(min(delay, 5.0))
            delay *= 1.5
    raise RuntimeError(f"Could not connect to MongoDB: {last_error}")


async def close_client() -> None:
    """Close the active client (no-op if nothing is set)."""
    if _state.client is not None:
        _state.client.close()
        _state.client = None
        _state.db = None
