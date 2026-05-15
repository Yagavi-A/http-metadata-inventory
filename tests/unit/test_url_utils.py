"""Tests for the URL normalisation helper."""

from __future__ import annotations

import pytest

from app.services.url_utils import InvalidURLError, normalize_url


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://example.com", "https://example.com/"),
        ("HTTPS://Example.COM/", "https://example.com/"),
        ("http://example.com:80/foo", "http://example.com/foo"),
        ("https://example.com:443/foo?a=1", "https://example.com/foo?a=1"),
        ("https://example.com:8443/foo", "https://example.com:8443/foo"),
        ("https://example.com/page#fragment", "https://example.com/page"),
        ("  https://example.com/x  ", "https://example.com/x"),
    ],
)
def test_normalize_url_canonical_forms(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "ftp://example.com",
        "example.com",
        "https://",
    ],
)
def test_normalize_url_rejects_invalid_inputs(bad: str) -> None:
    with pytest.raises(InvalidURLError):
        normalize_url(bad)


def test_normalize_url_rejects_non_string() -> None:
    with pytest.raises(InvalidURLError):
        normalize_url(None)  # type: ignore[arg-type]
