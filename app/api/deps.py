"""FastAPI dependency providers.

Keeping these in one place keeps the route handlers small and makes it
trivial to override collaborators in tests.
"""

from __future__ import annotations

from fastapi import Depends, Request

from app.core.config import Settings, get_settings
from app.db.mongo import get_collection
from app.services.fetcher import HTTPFetcher
from app.services.metadata_service import MetadataService
from app.services.url_security import URLSecurityPolicy
from app.storage.metadata import MetadataRepository
from app.workers.background import BackgroundWorker, get_worker


def get_repository(settings: Settings = Depends(get_settings)) -> MetadataRepository:
    """Build a :class:`MetadataRepository` bound to the configured collection."""
    return MetadataRepository(get_collection(settings.mongo_collection))


def get_fetcher(request: Request) -> HTTPFetcher:
    """Return the shared :class:`HTTPFetcher` attached to ``app.state``."""
    fetcher = getattr(request.app.state, "fetcher", None)
    if fetcher is None:
        raise RuntimeError("HTTP fetcher is not initialised on the app state")
    return fetcher


def get_security_policy(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> URLSecurityPolicy:
    """Return the URL safety policy (cached on ``app.state`` when available)."""
    policy = getattr(request.app.state, "security_policy", None)
    if policy is not None:
        return policy
    return URLSecurityPolicy(
        block_private_networks=settings.fetch_block_private_networks,
        blocked_hosts=settings.blocked_hosts_set,
    )


def get_metadata_service(
    repo: MetadataRepository = Depends(get_repository),
    fetcher: HTTPFetcher = Depends(get_fetcher),
    policy: URLSecurityPolicy = Depends(get_security_policy),
) -> MetadataService:
    """Compose a per-request :class:`MetadataService`."""
    return MetadataService(repository=repo, fetcher=fetcher, security_policy=policy)


def get_background_worker() -> BackgroundWorker:
    return get_worker()
