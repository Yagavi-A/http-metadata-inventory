"""Tests for the SSRF guard in ``app.services.url_security``."""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from app.services.url_security import (
    UnsafeURLError,
    URLSecurityPolicy,
    assert_url_is_safe,
)

pytestmark = pytest.mark.asyncio


def _fake_getaddrinfo(addresses: list[str]):
    """Return a stub that fakes ``loop.getaddrinfo`` with the given IPs."""

    async def fake(host, port, *, proto=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, proto, "", (ip, 0)) for ip in addresses]

    return fake


async def test_assert_url_is_safe_allows_public_ip() -> None:
    """A public IP (e.g. 1.1.1.1) passes the SSRF check."""
    policy = URLSecurityPolicy()
    with patch("app.services.url_security.asyncio.get_running_loop") as get_loop:
        get_loop.return_value.getaddrinfo = _fake_getaddrinfo(["1.1.1.1"])
        await assert_url_is_safe("https://example.com/", policy)


async def test_assert_url_is_safe_allows_nat64_synthesised_address() -> None:
    """A NAT64 well-known-prefix address (RFC 6052) is allowed.

    Dual-stack / IPv6-only Docker networks synthesise these for public
    IPv4 services; rejecting them would block legitimate traffic.
    """
    policy = URLSecurityPolicy()
    with patch("app.services.url_security.asyncio.get_running_loop") as get_loop:
        # 64:ff9b::1717:db03 == NAT64-translation of 23.23.219.3 (httpbin.org)
        get_loop.return_value.getaddrinfo = _fake_getaddrinfo(["64:ff9b::1717:db03"])
        await assert_url_is_safe("https://httpbin.org/get", policy)


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # RFC1918
        "192.168.1.1",  # RFC1918
        "169.254.169.254",  # link-local (AWS metadata)
        "::1",  # IPv6 loopback
        "fe80::1",  # IPv6 link-local
    ],
)
async def test_assert_url_is_safe_rejects_private_ips(ip: str) -> None:
    """Any address in a private/loopback/link-local range is rejected."""
    policy = URLSecurityPolicy()
    with patch("app.services.url_security.asyncio.get_running_loop") as get_loop:
        get_loop.return_value.getaddrinfo = _fake_getaddrinfo([ip])
        with pytest.raises(UnsafeURLError):
            await assert_url_is_safe("https://internal.example/", policy)


async def test_assert_url_is_safe_rejects_blocked_host() -> None:
    """Hostnames listed in ``blocked_hosts`` are rejected even before DNS."""
    policy = URLSecurityPolicy(
        block_private_networks=False,
        blocked_hosts=frozenset({"forbidden.example"}),
    )
    with pytest.raises(UnsafeURLError, match="deny list"):
        await assert_url_is_safe("https://forbidden.example/path", policy)


async def test_assert_url_is_safe_skips_dns_when_disabled() -> None:
    """When ``block_private_networks`` is off, no DNS lookup happens."""
    policy = URLSecurityPolicy(block_private_networks=False)
    # If we accidentally resolved, the stub below would raise.
    with patch("app.services.url_security.asyncio.get_running_loop") as get_loop:

        async def boom(*_a, **_kw):
            raise AssertionError("DNS should not be consulted")

        get_loop.return_value.getaddrinfo = boom
        await assert_url_is_safe("https://example.com/", policy)


async def test_assert_url_is_safe_translates_dns_failure() -> None:
    """A resolver error surfaces as :class:`UnsafeURLError`, not a raw socket error."""
    policy = URLSecurityPolicy()
    with patch("app.services.url_security.asyncio.get_running_loop") as get_loop:

        async def fail(*_a, **_kw):
            raise socket.gaierror(-2, "Name or service not known")

        get_loop.return_value.getaddrinfo = fail
        with pytest.raises(UnsafeURLError, match="DNS lookup failed"):
            await assert_url_is_safe("https://nope.invalid/", policy)
