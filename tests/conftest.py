"""Shared pytest fixtures.

We bypass the real ``app.main.lifespan`` in tests: a Mongo client is
wired into the global Mongo state (``mongomock-motor`` by default,
or a real Motor client when ``MONGO_TEST_URI`` is set), a deterministic
:class:`FakeFetcher` replaces the network-bound ``HTTPFetcher``, and a
real :class:`BackgroundWorker` is started so the asynchronous workflow
is exercised end-to-end.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from mongomock_motor import AsyncMongoMockClient

from app.api.middleware import RequestIdMiddleware
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.db.mongo import close_client, set_client
from app.services.fetcher import FetchError, FetchResult
from app.services.metadata_service import MetadataService
from app.services.url_security import URLSecurityPolicy
from app.storage.metadata import MetadataRepository
from app.workers.background import BackgroundWorker, FetchJob, set_worker

TEST_DB_NAME = "test_metadata_db"
PERMISSIVE_POLICY = URLSecurityPolicy(block_private_networks=False)


def _real_mongo_uri() -> str | None:
    """Return the real-Mongo URI to use in tests, if configured."""
    return os.getenv("MONGO_TEST_URI") or None


class FakeFetcher:
    """In-memory stand-in for :class:`HTTPFetcher`.

    Tests configure responses (or exceptions) per URL and can introduce
    an artificial delay to exercise the asynchronous code paths.
    """

    def __init__(self) -> None:
        self._responses: dict[str, FetchResult | Exception] = {}
        self.calls: list[str] = []
        self.delay: float = 0.0

    def set_result(self, url: str, result: FetchResult) -> None:
        self._responses[url] = result

    def set_error(self, url: str, exc: Exception) -> None:
        self._responses[url] = exc

    async def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        if self.delay:
            await asyncio.sleep(self.delay)
        if url not in self._responses:
            raise FetchError(f"No fake response configured for {url}")
        outcome = self._responses[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def _drop_db(client: Any, db_name: str) -> None:
    db = client[db_name]
    for name in await db.list_collection_names():
        await db[name].drop()


@pytest_asyncio.fixture
async def mongo_client() -> AsyncIterator[Any]:
    """Yield a Mongo client.

    Uses ``mongomock-motor`` for fast unit/integration tests, or a real
    Motor client when ``MONGO_TEST_URI`` is exported (CI's "real Mongo"
    job).
    """
    real_uri = _real_mongo_uri()
    if real_uri:
        # Defer the import so projects without the real driver still load.
        from motor.motor_asyncio import AsyncIOMotorClient

        client = AsyncIOMotorClient(real_uri)
        await _drop_db(client, TEST_DB_NAME)
        set_client(client, TEST_DB_NAME)
        try:
            yield client
        finally:
            await _drop_db(client, TEST_DB_NAME)
            await close_client()
    else:
        client = AsyncMongoMockClient()
        set_client(client, TEST_DB_NAME)
        try:
            yield client
        finally:
            await close_client()


@pytest_asyncio.fixture
async def repository(mongo_client: Any) -> MetadataRepository:
    settings = get_settings()
    collection = mongo_client[TEST_DB_NAME][settings.mongo_collection]
    repo = MetadataRepository(collection)
    # mongomock implements indexes as no-ops in some versions; ignore failures.
    with contextlib.suppress(Exception):
        await repo.ensure_indexes()
    return repo


@pytest.fixture
def fake_fetcher() -> FakeFetcher:
    return FakeFetcher()


@pytest_asyncio.fixture
async def worker(
    repository: MetadataRepository, fake_fetcher: FakeFetcher
) -> AsyncIterator[BackgroundWorker]:
    service = MetadataService(
        repository=repository,
        fetcher=fake_fetcher,  # type: ignore[arg-type]
        security_policy=PERMISSIVE_POLICY,
    )

    async def handler(job: FetchJob) -> None:
        await service.collect_in_background(
            normalized_url=job.normalized_url,
            original_url=job.original_url,
        )

    w = BackgroundWorker(handler=handler, concurrency=2, queue_maxsize=100)
    await w.start()
    set_worker(w)
    try:
        yield w
    finally:
        await w.stop()
        set_worker(None)


@pytest_asyncio.fixture
async def app(
    mongo_client: Any,
    fake_fetcher: FakeFetcher,
    worker: BackgroundWorker,
) -> FastAPI:
    application = FastAPI(title="Test HTTP Metadata Inventory")
    application.add_middleware(RequestIdMiddleware)
    application.include_router(api_router)
    application.state.fetcher = fake_fetcher
    application.state.security_policy = PERMISSIVE_POLICY
    return application


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
