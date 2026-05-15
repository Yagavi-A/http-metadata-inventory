"""URL normalization helpers.

A canonical representation lets us use the URL as the Mongo `_id` and
guarantees that `https://Example.com` and `https://example.com/` map to the
same metadata document.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


class InvalidURLError(ValueError):
    """Raised when a string cannot be interpreted as a fetchable URL."""


_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_url(url: str) -> str:
    """Return a canonical form of *url*.

    Rules:
        * scheme and host are lower-cased;
        * only ``http`` and ``https`` are accepted;
        * default ports (80/443) are dropped from the netloc;
        * an empty path becomes ``/``;
        * fragments are stripped (they are not sent over the wire).
    """
    if not isinstance(url, str) or not url.strip():
        raise InvalidURLError("URL must be a non-empty string")

    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise InvalidURLError("Only http and https URLs are supported")

    host = (parts.hostname or "").lower()
    if not host:
        raise InvalidURLError("URL is missing a host")

    port = parts.port
    netloc = host if port is None or port == _DEFAULT_PORTS[scheme] else f"{host}:{port}"

    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))
