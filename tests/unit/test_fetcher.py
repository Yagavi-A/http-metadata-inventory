"""Unit tests for :class:`app.services.fetcher.HTTPFetcher`.

We mount a respx mock on a real ``httpx.AsyncClient`` so the assertions
exercise the same code path used in production.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.services.fetcher import FetcherConfig, FetchError, HTTPFetcher
from app.services.url_security import URLSecurityPolicy

pytestmark = pytest.mark.asyncio

_PERMISSIVE = URLSecurityPolicy(block_private_networks=False)


@pytest.fixture
def fetcher_config() -> FetcherConfig:
    return FetcherConfig(
        timeout_seconds=2.0,
        max_redirects=2,
        user_agent="ua-test/1.0",
        security_policy=_PERMISSIVE,
    )


async def test_fetch_returns_headers_cookies_and_body(
    fetcher_config: FetcherConfig,
) -> None:
    """A successful response is decomposed into headers, cookies, and page source."""
    url = "https://example.com/"
    response = httpx.Response(
        200,
        headers=[
            ("content-type", "text/html"),
            ("set-cookie", "session=abc; Path=/; Domain=example.com"),
        ],
        text="<html>hi</html>",
    )
    with respx.mock(assert_all_called=True) as router:
        router.get(url).mock(return_value=response)
        client = HTTPFetcher.build_client(fetcher_config)
        try:
            fetcher = HTTPFetcher(client, fetcher_config)
            result = await fetcher.fetch(url)
        finally:
            await client.aclose()

    assert result.http_status == 200
    assert result.headers["content-type"] == "text/html"
    assert result.page_source == "<html>hi</html>"
    assert any(c["name"] == "session" and c["value"] == "abc" for c in result.cookies)


async def test_fetch_translates_timeout_to_fetch_error(
    fetcher_config: FetcherConfig,
) -> None:
    """``httpx`` timeouts are surfaced to callers as our domain ``FetchError``."""
    url = "https://example.com/"
    with respx.mock() as router:
        router.get(url).mock(side_effect=httpx.TimeoutException("nope"))
        client = HTTPFetcher.build_client(fetcher_config)
        try:
            fetcher = HTTPFetcher(client, fetcher_config)
            with pytest.raises(FetchError):
                await fetcher.fetch(url)
        finally:
            await client.aclose()


async def test_fetch_translates_connection_error(
    fetcher_config: FetcherConfig,
) -> None:
    """Connection-level errors (DNS/TCP) are also normalised to ``FetchError``."""
    url = "https://example.com/"
    with respx.mock() as router:
        router.get(url).mock(side_effect=httpx.ConnectError("dns"))
        client = HTTPFetcher.build_client(fetcher_config)
        try:
            fetcher = HTTPFetcher(client, fetcher_config)
            with pytest.raises(FetchError):
                await fetcher.fetch(url)
        finally:
            await client.aclose()


async def test_fetch_rejects_oversize_content_length(fetcher_config: FetcherConfig) -> None:
    """A ``Content-Length`` header that exceeds the cap is rejected before the body is read."""
    url = "https://example.com/"
    fetcher_config.max_body_bytes = 1024
    response = httpx.Response(
        200,
        headers=[("content-length", "10000"), ("content-type", "text/html")],
        text="x" * 100,  # body length is irrelevant; we trip on the header
    )
    with respx.mock() as router:
        router.get(url).mock(return_value=response)
        client = HTTPFetcher.build_client(fetcher_config)
        try:
            fetcher = HTTPFetcher(client, fetcher_config)
            with pytest.raises(FetchError, match="exceeds cap"):
                await fetcher.fetch(url)
        finally:
            await client.aclose()


async def test_fetch_rejects_streamed_body_over_cap(fetcher_config: FetcherConfig) -> None:
    """If a server lies about Content-Length, the streaming reader still trips the cap."""
    url = "https://example.com/"
    fetcher_config.max_body_bytes = 16

    response = httpx.Response(200, content=b"A" * 1024)
    # Strip Content-Length so the pre-check is skipped and the streaming
    # reader is what actually trips the cap.
    response.headers.pop("content-length", None)
    with respx.mock() as router:
        router.get(url).mock(return_value=response)
        client = HTTPFetcher.build_client(fetcher_config)
        try:
            fetcher = HTTPFetcher(client, fetcher_config)
            with pytest.raises(FetchError, match="exceeded cap"):
                await fetcher.fetch(url)
        finally:
            await client.aclose()
