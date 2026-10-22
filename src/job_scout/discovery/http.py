"""Pinned public-IP transport for fixed, documented public API hosts."""

import asyncio
import ipaddress
import socket

import httpx

from job_scout.sources.base import SourceError

API_HOSTS = {"boards-api.greenhouse.io", "api.lever.co", "api.eu.lever.co", "api.ashbyhq.com"}


async def resolve(host: str) -> list[str]:
    try:
        async with asyncio.timeout(10):
            records = await asyncio.get_running_loop().getaddrinfo(
                host,
                443,
                type=socket.SOCK_STREAM,
            )
    except (OSError, TimeoutError):
        raise SourceError("Public API DNS lookup failed; retry later") from None
    return list(dict.fromkeys(record[4][0] for record in records))


class PublicAPITransport(httpx.AsyncBaseTransport):
    """Resolve once per request and connect to that IP, preserving TLS hostname checks.

    Restrict both the hostname and every resolved address. Pinning closes the gap
    between a DNS preflight and a second DNS lookup at connection time.
    """

    def __init__(self, *, transport=None, resolver=resolve, allowed_hosts=None):
        self.transport = transport if transport is not None else httpx.AsyncHTTPTransport()
        self.resolver = resolver
        self.allowed_hosts = frozenset(API_HOSTS if allowed_hosts is None else allowed_hosts)

    async def handle_async_request(self, request):
        host = request.url.host
        if (
            request.method != "GET"
            or request.url.scheme != "https"
            or request.url.port not in {None, 443}
            or host not in self.allowed_hosts
        ):
            raise SourceError("Discovery only permits HTTPS GET to approved public API hosts")
        addresses = await self.resolver(host)
        if not addresses:
            raise SourceError("API host has no public address")
        for value in addresses:
            address = ipaddress.ip_address(value)
            if not address.is_global or address.is_multicast:
                raise SourceError("API hostname resolved to a non-public address")
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=addresses[0]),
            headers=request.headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": host},
        )
        return await self.transport.handle_async_request(pinned)

    async def aclose(self):
        await self.transport.aclose()


def public_api_client(*, allowed_hosts: set[str] | None = None) -> httpx.AsyncClient:
    """Consistent, credential-free client for allowlisted public JSON APIs."""
    return httpx.AsyncClient(
        transport=PublicAPITransport(allowed_hosts=allowed_hosts),
        trust_env=False,
        follow_redirects=False,
        headers={"User-Agent": "JobScout/0.1", "Accept": "application/json"},
    )
