"""Async HTTP fetcher used by the metadata service.

The fetcher is a thin wrapper around a shared ``httpx.AsyncClient`` so
that connection pooling is reused across the worker pool.  It only deals
with *static* responses: JavaScript rendering is explicitly out of scope.

Two safety nets live here:

* an optional :class:`URLSecurityPolicy` check so the fetcher refuses
  obviously-unsafe targets even if the service layer forgets to;
* a streaming body reader that aborts mid-download when the configured
  ``max_body_bytes`` cap is hit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from app.services.rate_limiter import NoOpRateLimiter, RateLimiter
from app.services.url_security import UnsafeURLError, URLSecurityPolicy, assert_url_is_safe

logger = logging.getLogger(__name__)


class FetchError(Exception):
    """Raised when the upstream URL cannot be fetched."""


@dataclass(slots=True)
class FetchResult:
    """Outcome of a successful fetch."""

    http_status: int
    headers: dict[str, str]
    cookies: list[dict[str, str | None]]
    page_source: str
    final_url: str = ""


@dataclass(slots=True)
class FetcherConfig:
    """Tunable knobs for the HTTP fetcher (timeouts, redirects, UA, body cap)."""

    timeout_seconds: float = 10.0
    max_redirects: int = 5
    user_agent: str = "http-metadata-inventory/0.1"
    max_body_bytes: int = 5 * 1024 * 1024  # 5 MiB safety cap
    security_policy: URLSecurityPolicy = field(default_factory=URLSecurityPolicy)

    @property
    def default_headers(self) -> dict[str, str]:
        """Default request headers; merged with whatever httpx adds."""
        return {"User-Agent": self.user_agent, "Accept": "*/*"}


class HTTPFetcher:
    """Stateful fetcher backed by a single :class:`httpx.AsyncClient`.

    An optional :class:`RateLimiter` controls per-host concurrency; the
    default no-op limiter keeps behaviour identical to a plain fetcher.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        config: FetcherConfig,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._client = client
        self._config = config
        self._rate_limiter: RateLimiter = rate_limiter or NoOpRateLimiter()

    @classmethod
    def build_client(cls, config: FetcherConfig) -> httpx.AsyncClient:
        """Construct a long-lived ``httpx.AsyncClient`` with pooling + redirects."""
        timeout = httpx.Timeout(config.timeout_seconds, connect=config.timeout_seconds)
        limits = httpx.Limits(max_connections=100, max_keepalive_connections=20)
        return httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            follow_redirects=True,
            max_redirects=config.max_redirects,
            headers=config.default_headers,
        )

    async def fetch(self, url: str) -> FetchResult:
        """GET *url* and return its headers, cookies, and body as a :class:`FetchResult`.

        Network failures are normalised into :class:`FetchError` so the caller
        can react uniformly regardless of which httpx exception fired.  An
        unsafe target (per :class:`URLSecurityPolicy`) raises
        :class:`~app.services.url_security.UnsafeURLError`, which the route
        layer translates into an HTTP 400.
        """
        await assert_url_is_safe(url, self._config.security_policy)

        host = urlsplit(url).hostname or ""
        max_bytes = self._config.max_body_bytes
        async with self._rate_limiter.limit(host):
            try:
                async with self._client.stream("GET", url) as response:
                    self._reject_if_content_length_too_big(url, response, max_bytes)
                    body = await self._read_capped_body(url, response, max_bytes)
                    headers = dict(response.headers.items())
                    cookies = [
                        {
                            "name": c.name,
                            "value": c.value,
                            "domain": c.domain,
                            "path": c.path,
                        }
                        for c in response.cookies.jar
                    ]
                    encoding = response.encoding or "utf-8"
                    final_url = str(response.url)
                    status_code = response.status_code
            except UnsafeURLError:
                raise
            except httpx.TimeoutException as exc:
                raise FetchError(f"Timed out while fetching {url}") from exc
            except httpx.TooManyRedirects as exc:
                raise FetchError(f"Too many redirects while fetching {url}") from exc
            except httpx.RequestError as exc:
                raise FetchError(f"Request failed for {url}: {exc}") from exc

        try:
            text = body.decode(encoding, errors="replace")
        except LookupError:
            # Unknown encoding label; fall back to utf-8.
            text = body.decode("utf-8", errors="replace")

        return FetchResult(
            http_status=status_code,
            headers=headers,
            cookies=cookies,
            page_source=text,
            final_url=final_url,
        )

    @staticmethod
    def _reject_if_content_length_too_big(
        url: str, response: httpx.Response, max_bytes: int
    ) -> None:
        raw = response.headers.get("content-length")
        if not raw:
            return
        try:
            advertised = int(raw)
        except ValueError:
            return
        if advertised > max_bytes:
            raise FetchError(
                f"Refusing to download {url}: Content-Length {advertised} "
                f"exceeds cap of {max_bytes} bytes"
            )

    @staticmethod
    async def _read_capped_body(url: str, response: httpx.Response, max_bytes: int) -> bytes:
        buf = bytearray()
        async for chunk in response.aiter_bytes():
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise FetchError(f"Body for {url} exceeded cap of {max_bytes} bytes")
        return bytes(buf)
