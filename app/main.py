"""FastAPI application entry point.

The lifespan context wires Mongo, the shared ``httpx.AsyncClient``, and
the background worker pool.  Routes are mounted under ``/v1``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.middleware import RequestIdMiddleware
from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.mongo import (
    close_client,
    connect_with_retry,
    get_collection,
    set_client,
)
from app.services.fetcher import FetcherConfig, HTTPFetcher
from app.services.metadata_service import MetadataService
from app.services.rate_limiter import build_rate_limiter
from app.services.url_security import URLSecurityPolicy
from app.storage.metadata import MetadataRepository
from app.workers.background import (
    BackgroundWorker,
    FetchJob,
    JobHandler,
    set_worker,
)

logger = logging.getLogger(__name__)


def _build_handler(
    service_factory: Callable[[], MetadataService],
) -> JobHandler:
    """Adapt a :class:`FetchJob` to :meth:`MetadataService.collect_in_background`."""

    async def handler(job: FetchJob) -> None:
        service = service_factory()
        await service.collect_in_background(
            normalized_url=job.normalized_url,
            original_url=job.original_url,
        )

    return handler


async def _resume_pending_jobs(repository: MetadataRepository, worker: BackgroundWorker) -> None:
    """Re-enqueue any ``pending`` records left over from a previous process.

    The worker queue is in-memory and does not survive a restart, so a
    document that was scheduled but not completed before the last shutdown
    would otherwise stay pending forever.
    """
    stuck = await repository.list_pending()
    if not stuck:
        return
    logger.info("Resuming %d pending record(s) from previous run", len(stuck))
    for record in stuck:
        await worker.submit(
            FetchJob(
                normalized_url=record.normalized_url,
                original_url=record.url,
            )
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start Mongo, the HTTP client, and the worker pool; tear them down on exit."""
    settings: Settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_format == "json")
    logger.info("Starting %s (%s)", settings.app_name, settings.app_env)

    client = await connect_with_retry(settings)
    set_client(client, settings.mongo_db)

    security_policy = URLSecurityPolicy(
        block_private_networks=settings.fetch_block_private_networks,
        blocked_hosts=settings.blocked_hosts_set,
    )
    fetcher_config = FetcherConfig(
        timeout_seconds=settings.fetch_timeout_seconds,
        max_redirects=settings.fetch_max_redirects,
        user_agent=settings.fetch_user_agent,
        max_body_bytes=settings.fetch_max_body_bytes,
        security_policy=security_policy,
    )
    http_client = HTTPFetcher.build_client(fetcher_config)
    rate_limiter = build_rate_limiter(settings.fetch_per_domain_concurrency)
    fetcher = HTTPFetcher(http_client, fetcher_config, rate_limiter=rate_limiter)
    app.state.fetcher = fetcher
    app.state.security_policy = security_policy

    repository = MetadataRepository(get_collection(settings.mongo_collection))
    await repository.ensure_indexes()

    def service_factory() -> MetadataService:
        # Recreate per-job so we never reuse a stale collection handle if
        # the lifespan ever recycles the underlying client.
        repo = MetadataRepository(get_collection(settings.mongo_collection))
        return MetadataService(repository=repo, fetcher=fetcher, security_policy=security_policy)

    worker = BackgroundWorker(
        handler=_build_handler(service_factory),
        concurrency=settings.worker_concurrency,
        queue_maxsize=settings.worker_queue_maxsize,
    )
    await worker.start()
    set_worker(worker)

    if settings.worker_resume_pending_on_startup:
        try:
            await _resume_pending_jobs(repository, worker)
        except Exception:  # noqa: BLE001 - never block startup on bookkeeping
            logger.exception("Failed to resume pending jobs at startup")

    try:
        yield
    finally:
        logger.info("Shutting down")
        await worker.stop()
        set_worker(None)
        await http_client.aclose()
        await close_client()


def create_app() -> FastAPI:
    """Construct the FastAPI application with routes and lifespan wired in."""
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_format == "json")
    app = FastAPI(
        title="HTTP Metadata Inventory",
        version="0.1.0",
        description=(
            "Collects HTTP response headers, cookies and page source for "
            "user-supplied URLs and stores them in MongoDB."
        ),
        lifespan=lifespan,
        # Swagger UI at /docs is the canonical API explorer; ReDoc is
        # intentionally disabled to keep the public surface small.
        redoc_url=None,
    )
    app.add_middleware(RequestIdMiddleware)
    app.include_router(api_router)
    return app


app = create_app()
