"""Shared source contract and respectful HTTP transport."""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol

import httpx

from job_scout.models import Job, SourceConfig


class SourceError(Exception):
    """An employer collection could not be completed safely."""


class JobSource(Protocol):
    async def fetch_jobs(self, config: SourceConfig) -> list[Job]: ...


class BoardHTTP:
    """Shared request pacing, finite retries and explicit request timeouts."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        attempts: int = 3,
        interval: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_bytes: int = 10_000_000,
        max_requests: int = 300,
        minimum_rate_limit_delay: float = 0,
        before_request: Callable[[], None] | None = None,
        after_response: Callable[[httpx.Response], None] | None = None,
    ) -> None:
        if attempts < 1 or interval < 0:
            raise ValueError("attempts must be positive and interval nonnegative")
        if max_bytes < 1 or max_requests < 1:
            raise ValueError("Response and request budgets must be positive")
        self.before_request = before_request
        self.after_response = after_response
        self.minimum_rate_limit_delay = minimum_rate_limit_delay
        self.max_bytes = max_bytes
        self.max_requests = max_requests
        self.requests = 0
        self.client = client
        self.attempts = attempts
        self.interval = interval
        self.sleep = sleep
        self._lock = asyncio.Lock()
        self._next_request = 0.0

    async def _pace(self) -> None:
        async with self._lock:
            await self.sleep(max(0.0, self._next_request - time.monotonic()))
            self._next_request = time.monotonic() + self.interval

    def _retry_after(self, response: httpx.Response) -> float:
        value = response.headers.get("Retry-After")
        if not value:
            return 0.0
        try:
            delay = float(value)
        except ValueError:
            try:
                delay = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                return 0.0
        # Do not retry earlier than the server requests; defer long waits to a later run.
        if delay > 60:
            raise SourceError("Server requested a long Retry-After; retry collection later")
        return max(0.0, delay)

    async def get_json(self, url: str, params: dict[str, str | int]) -> object:
        for attempt in range(self.attempts):
            await self._pace()
            delay = 0.0
            try:
                if self.requests >= self.max_requests:
                    raise SourceError("Request budget exhausted; retry later with a larger budget")
                if self.before_request:
                    self.before_request()
                self.requests += 1
                async with self.client.stream(
                    "GET",
                    url,
                    params=params,
                    timeout=20.0,
                    follow_redirects=False,
                    headers={"Accept-Encoding": "identity"},
                ) as response:
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise SourceError("Compressed response refused; request uncompressed JSON")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > self.max_bytes:
                            raise SourceError("Response exceeds the configured size limit")
                    response = httpx.Response(
                        response.status_code,
                        headers=response.headers,
                        content=bytes(body),
                        request=response.request,
                    )
            except httpx.TransportError as exc:
                reason = f"Transport failure: {type(exc).__name__}"
            else:
                if self.after_response:
                    self.after_response(response)
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    reason = f"HTTP {response.status_code} after retries"
                    delay = self._retry_after(response)
                    if response.status_code == 429:
                        delay = max(delay, self.minimum_rate_limit_delay)
                elif not response.is_success:
                    raise SourceError(f"HTTP {response.status_code}; check board ID and region")
                else:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise SourceError("Response is not valid JSON") from exc
            if attempt == self.attempts - 1:
                raise SourceError(reason)
            await self.sleep(max(delay, 2**attempt + random.uniform(0, 0.5)))
        raise AssertionError("Unreachable")
