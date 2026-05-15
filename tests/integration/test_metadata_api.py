"""End-to-end tests for the FastAPI app.

These exercise the real routers, dependencies, repository (mongomock)
and background worker.  The fetcher is the fake one wired in via
``conftest.py``.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.fetcher import FetchResult

pytestmark = pytest.mark.asyncio


async def test_healthz_reports_mongo_status(client) -> None:
    """``/healthz`` returns an overall status plus the Mongo subcomponent state."""
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in {"ok", "degraded"}
    assert "mongo" in body


async def test_livez_returns_204(client) -> None:
    """``/livez`` is the liveness probe and returns 204 with no body."""
    resp = await client.get("/livez")
    assert resp.status_code == 204


async def test_post_creates_record_synchronously(client, fake_fetcher) -> None:
    """POST fetches the URL inline and returns the full record with HTTP 201."""
    fake_fetcher.set_result(
        "https://example.com/",
        FetchResult(
            http_status=200,
            headers={"content-type": "text/html"},
            cookies=[{"name": "k", "value": "v", "domain": "example.com", "path": "/"}],
            page_source="<html>hi</html>",
            final_url="https://example.com/",
        ),
    )

    resp = await client.post("/v1/metadata", json={"url": "https://example.com"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "completed"
    assert body["normalized_url"] == "https://example.com/"
    assert body["http_status"] == 200
    assert body["headers"]["content-type"] == "text/html"
    assert body["page_source"] == "<html>hi</html>"
    assert body["cookies"][0]["name"] == "k"


async def test_post_invalid_url_returns_422(client) -> None:
    """Malformed URL bodies are rejected by Pydantic with 422 before our code runs."""
    resp = await client.post("/v1/metadata", json={"url": "not-a-url"})
    assert resp.status_code == 422


async def test_post_upstream_failure_returns_502(client, fake_fetcher) -> None:
    """POST surfaces upstream fetch failures as HTTP 502 with the error message."""
    from app.services.fetcher import FetchError

    fake_fetcher.set_error("https://example.com/", FetchError("timeout"))

    resp = await client.post("/v1/metadata", json={"url": "https://example.com"})
    assert resp.status_code == 502
    assert "timeout" in resp.json()["detail"]


async def test_get_returns_cached_record(client, fake_fetcher) -> None:
    """GET on a previously-POSTed URL returns the stored payload (cache hit)."""
    fake_fetcher.set_result(
        "https://example.com/",
        FetchResult(http_status=200, headers={"a": "b"}, cookies=[], page_source="hi"),
    )
    await client.post("/v1/metadata", json={"url": "https://example.com"})

    resp = await client.get("/v1/metadata", params={"url": "https://example.com"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["page_source"] == "hi"


async def test_get_on_miss_returns_202_and_schedules_collection(
    client, fake_fetcher, worker
) -> None:
    """GET on a cache miss returns 202; the worker then fills the record asynchronously."""
    fake_fetcher.set_result(
        "https://example.com/",
        FetchResult(
            http_status=200,
            headers={"server": "nginx"},
            cookies=[],
            page_source="ok",
            final_url="https://example.com/",
        ),
    )

    resp = await client.get("/v1/metadata", params={"url": "https://example.com"})
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "pending"
    assert body["normalized_url"] == "https://example.com/"

    await worker.join()

    # The background worker should have completed the record by now.
    follow_up = await client.get("/v1/metadata", params={"url": "https://example.com"})
    assert follow_up.status_code == 200
    completed = follow_up.json()
    assert completed["status"] == "completed"
    assert completed["page_source"] == "ok"
    assert completed["headers"]["server"] == "nginx"


async def test_get_invalid_url_returns_400(client) -> None:
    """GET with an unsupported scheme (e.g. ftp://) is rejected with HTTP 400."""
    resp = await client.get("/v1/metadata", params={"url": "ftp://example.com"})
    assert resp.status_code == 400


async def test_concurrent_get_misses_only_schedule_once(client, fake_fetcher, worker) -> None:
    """Concurrent GET misses for the same URL all 202 but trigger a single upstream fetch."""
    fake_fetcher.delay = 0.05
    fake_fetcher.set_result(
        "https://example.com/",
        FetchResult(http_status=200, headers={}, cookies=[], page_source="x"),
    )

    responses = await asyncio.gather(
        *[client.get("/v1/metadata", params={"url": "https://example.com"}) for _ in range(5)]
    )
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [202, 202, 202, 202, 202]

    await worker.join()
    # The fetcher should have been called exactly once.
    assert fake_fetcher.calls == ["https://example.com/"]


async def test_get_normalises_url_for_lookup(client, fake_fetcher, worker) -> None:
    """GET normalises scheme/host casing and default ports, so equivalent URLs hit the same record."""
    fake_fetcher.set_result(
        "https://example.com/",
        FetchResult(http_status=200, headers={}, cookies=[], page_source="ok"),
    )
    await client.post("/v1/metadata", json={"url": "https://example.com"})

    resp = await client.get("/v1/metadata", params={"url": "HTTPS://EXAMPLE.com:443/"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"


async def test_responses_carry_request_id_header(client) -> None:
    """Every response is tagged with an ``X-Request-ID`` header generated by middleware."""
    resp = await client.get("/livez")
    assert "x-request-id" in {k.lower() for k in resp.headers}
    assert resp.headers["x-request-id"]


async def test_response_echoes_inbound_request_id(client) -> None:
    """If the client supplies ``X-Request-ID``, the server echoes the same value back."""
    rid = "abc-123-test"
    resp = await client.get("/livez", headers={"X-Request-ID": rid})
    assert resp.headers["x-request-id"] == rid


async def test_get_with_max_age_refreshes_stale_record(client, fake_fetcher, worker) -> None:
    """``?max_age=0`` forces re-collection of a completed record and returns 202."""
    fake_fetcher.set_result(
        "https://example.com/",
        FetchResult(http_status=200, headers={}, cookies=[], page_source="v1"),
    )
    # First POST stores a completed record.
    await client.post("/v1/metadata", json={"url": "https://example.com"})
    # Configure a new payload for the upcoming refresh.
    fake_fetcher.set_result(
        "https://example.com/",
        FetchResult(http_status=200, headers={}, cookies=[], page_source="v2"),
    )

    resp = await client.get("/v1/metadata", params={"url": "https://example.com", "max_age": 0})
    assert resp.status_code == 202
    assert resp.json()["status"] == "pending"

    await worker.join()

    final = await client.get("/v1/metadata", params={"url": "https://example.com"})
    assert final.status_code == 200
    assert final.json()["page_source"] == "v2"


async def test_get_with_max_age_serves_fresh_record(client, fake_fetcher) -> None:
    """A completed record younger than ``max_age`` is served as a normal cache hit."""
    fake_fetcher.set_result(
        "https://example.com/",
        FetchResult(http_status=200, headers={}, cookies=[], page_source="fresh"),
    )
    await client.post("/v1/metadata", json={"url": "https://example.com"})

    resp = await client.get("/v1/metadata", params={"url": "https://example.com", "max_age": 3600})
    assert resp.status_code == 200
    assert resp.json()["page_source"] == "fresh"
