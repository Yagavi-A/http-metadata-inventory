"""Application settings loaded from environment variables."""

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed application configuration sourced from env + ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "http-metadata-inventory"
    app_env: str = "development"
    log_level: str = "INFO"
    # "plain" (default) for human-readable logs, "json" for structured logs.
    log_format: str = Field(default="plain", pattern="^(plain|json)$")

    api_host: str = "0.0.0.0"
    api_port: int = 8000

    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "metadata_inventory"
    mongo_collection: str = "metadata"

    fetch_timeout_seconds: float = 10.0
    fetch_max_redirects: int = 5
    fetch_user_agent: str = "http-metadata-inventory/0.1"
    # 0 disables per-domain throttling; >= 1 caps concurrent in-flight
    # requests per host.  See ``app.services.rate_limiter``.
    fetch_per_domain_concurrency: int = Field(default=0, ge=0)
    # Hard cap on the response body we are willing to ingest, in bytes.
    fetch_max_body_bytes: int = Field(default=5 * 1024 * 1024, ge=1024)

    # SSRF defences.
    fetch_block_private_networks: bool = True
    # Comma-separated list of hostnames that should be rejected outright.
    fetch_blocked_hosts: str = ""

    worker_queue_maxsize: int = Field(default=1000, ge=1)
    worker_concurrency: int = Field(default=2, ge=1)
    # On startup, re-enqueue documents stuck in ``pending`` from a previous
    # run.  Useful because the queue is in-process and does not survive a
    # restart.
    worker_resume_pending_on_startup: bool = True

    @property
    def blocked_hosts_set(self) -> frozenset[str]:
        """Parse ``fetch_blocked_hosts`` into a normalised set of hostnames."""
        return frozenset(
            host.strip().lower() for host in self.fetch_blocked_hosts.split(",") if host.strip()
        )

    @field_validator("log_format")
    @classmethod
    def _lowercase_log_format(cls, value: str) -> str:
        return value.lower()


@lru_cache
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance (one per process)."""
    return Settings()
