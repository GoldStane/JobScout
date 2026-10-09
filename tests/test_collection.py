import asyncio

import httpx

from job_scout.collection import collect
from job_scout.models import SourceConfig, SourceRegistry


def test_partial_failure_preserves_success():
    registry = SourceRegistry(
        sources=[
            SourceConfig(source="greenhouse", company="Bad", board_id="bad", enabled=True),
            SourceConfig(source="lever", company="Good", board_id="good", enabled=True),
            SourceConfig(source="lever", company="Disabled", board_id="disabled"),
        ]
    )
    calls = []

    async def run():
        def handler(request):
            calls.append(request)
            if "greenhouse" in request.url.host:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "one",
                        "text": "Intern",
                        "hostedUrl": "https://example.org/job",
                        "applyUrl": "https://example.org/apply",
                    }
                ],
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await collect(registry, client=client, interval=0)

    report = asyncio.run(run())
    assert report.failed
    assert len(calls) == 2
    assert len(report.sources) == 2
    assert report.sources[0].error is not None
    assert report.sources[1].error is None
    assert len(report.sources[1].jobs) == 1
    assert all(s.completed_at >= s.attempted_at for s in report.sources)


def test_concurrency_is_bounded():
    registry = SourceRegistry(
        sources=[
            SourceConfig(source="greenhouse", company="Example", board_id=f"board{i}", enabled=True)
            for i in range(6)
        ]
    )
    active = peak = 0

    async def run():
        both_started = asyncio.Event()

        async def handler(request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)
            active -= 1
            return httpx.Response(200, json={"jobs": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await collect(registry, client=client, concurrency=2, interval=0)

    report = asyncio.run(run())
    assert not report.failed
    assert len(report.sources) == 6
    assert peak == 2
