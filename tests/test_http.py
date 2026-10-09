import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from job_scout.sources.base import BoardHTTP, SourceError


def request_sequence(outcomes, *, attempts=3):
    calls = []
    sleeps = []

    async def run():
        async def sleep(delay):
            sleeps.append(delay)

        def handler(request):
            calls.append(request)
            outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await BoardHTTP(client, interval=0, attempts=attempts, sleep=sleep).get_json(
                "https://example.org", {}
            )
            return result

    return run, calls, sleeps


@pytest.mark.parametrize(
    "first",
    [
        httpx.Response(429),
        httpx.Response(500),
        httpx.Response(503),
        httpx.ReadTimeout("timeout"),
        httpx.ConnectError("offline"),
    ],
)
def test_transient_failure_retried(first):
    run, calls, sleeps = request_sequence([first, httpx.Response(200, json=[])])
    assert asyncio.run(run()) == []
    assert len(calls) == 2
    assert any(1 <= delay <= 1.5 for delay in sleeps)


def test_retry_exhaustion():
    run, calls, sleeps = request_sequence([httpx.Response(503)])
    with pytest.raises(SourceError, match="HTTP 503"):
        asyncio.run(run())
    assert len(calls) == 3
    assert any(2 <= delay <= 2.5 for delay in sleeps)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 302])
def test_permanent_errors_not_retried(status):
    run, calls, _ = request_sequence([httpx.Response(status)])
    with pytest.raises(SourceError, match=f"HTTP {status}"):
        asyncio.run(run())
    assert len(calls) == 1


@pytest.mark.parametrize(
    "header", ["5", format_datetime(datetime.now(UTC) + timedelta(seconds=30))]
)
def test_retry_after_respected(header):
    run, calls, sleeps = request_sequence(
        [httpx.Response(429, headers={"Retry-After": header}), httpx.Response(200, json=[])]
    )
    asyncio.run(run())
    assert len(calls) == 2
    assert max(sleeps) >= 5


def test_long_retry_after_defers_without_early_retry():
    run, calls, _ = request_sequence([httpx.Response(429, headers={"Retry-After": "3600"})])
    with pytest.raises(SourceError, match="retry collection later"):
        asyncio.run(run())
    assert len(calls) == 1


def test_invalid_json_not_retried():
    run, calls, _ = request_sequence([httpx.Response(200, text="not json")])
    with pytest.raises(SourceError, match="not valid JSON"):
        asyncio.run(run())
    assert len(calls) == 1


def test_requests_are_paced():
    sleeps = []

    async def run():
        async def sleep(delay):
            sleeps.append(delay)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
        ) as client:
            http = BoardHTTP(client, interval=0.5, sleep=sleep)
            await asyncio.gather(
                http.get_json("https://example.org/a", {}),
                http.get_json("https://example.org/b", {}),
            )

    asyncio.run(run())
    assert len(sleeps) == 2
    assert 0 < sleeps[1] <= 0.5
