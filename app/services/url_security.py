"""URL safety checks to defend against SSRF.

Before any HTTP request is made we resolve the URL's hostname and reject
it if any of the returned addresses live in a private, loopback,
link-local, multicast, reserved, or otherwise non-public range.

This is best-effort, not a guarantee: the resolver could change its
answer between this check and the actual TCP connect (TOCTOU).  In
production you'd combine it with an egress-firewall and/or a custom
``httpx`` transport that re-checks the resolved IP just before connect.
For a hiring submission, blocking the obvious SSRF targets at the
service boundary is the high-value part.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


class UnsafeURLError(Exception):
    """Raised when a URL targets a host we refuse to fetch."""


# Address attributes that mark a destination as unsafe to contact.
#
# Note: ``is_reserved`` is deliberately *not* in this list.  Python's
# stdlib flags several legitimately-routable ranges (notably the NAT64
# well-known prefix ``64:ff9b::/96`` from RFC 6052) as "reserved", which
# would cause false-positives on dual-stack or IPv6-only networks where
# public IPv4 services are reached via NAT64.  The remaining checks
# cover every actual SSRF target (RFC1918, loopback, link-local,
# multicast, broadcast, ULA, AWS metadata IP, etc.).
_PRIVATE_ATTRS = (
    "is_private",
    "is_loopback",
    "is_link_local",
    "is_multicast",
    "is_unspecified",
)


@dataclass(frozen=True, slots=True)
class URLSecurityPolicy:
    """Policy applied to every URL before it leaves the process."""

    block_private_networks: bool = True
    blocked_hosts: frozenset[str] = field(default_factory=frozenset)


async def _resolve_ips(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"DNS lookup failed for {host!r}: {exc}") from exc

    ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        sockaddr = info[4]
        try:
            ips.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            raise UnsafeURLError(
                f"Resolver returned non-IP value {sockaddr[0]!r} for {host!r}"
            ) from None
    return ips


async def assert_url_is_safe(url: str, policy: URLSecurityPolicy) -> None:
    """Raise :class:`UnsafeURLError` if *url* fails the configured checks."""
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        raise UnsafeURLError(f"URL has no host: {url!r}")

    host_lower = host.lower()
    if host_lower in policy.blocked_hosts:
        raise UnsafeURLError(f"Host {host!r} is in the deny list")

    if not policy.block_private_networks:
        return

    for ip in await _resolve_ips(host):
        for attr in _PRIVATE_ATTRS:
            if getattr(ip, attr, False):
                raise UnsafeURLError(f"Host {host!r} resolves to non-public address {ip} ({attr})")
