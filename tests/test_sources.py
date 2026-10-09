import asyncio
import json
from datetime import UTC
from pathlib import Path

import httpx
import pytest

from job_scout.models import SourceConfig
from job_scout.sources.ashby import AshbySource
from job_scout.sources.base import BoardHTTP, SourceError
from job_scout.sources.greenhouse import GreenhouseSource
from job_scout.sources.lever import LeverSource

FIXTURES = Path(__file__).parent / "fixtures"


def payload(provider):
    return json.loads((FIXTURES / f"{provider}.json").read_text())


def config(provider, **kwargs):
    return SourceConfig(
        source=provider, company="Example", board_id="example", enabled=True, **kwargs
    )


def fetch(provider, body, **kwargs):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
        ) as client:
            adapter = {
                "greenhouse": GreenhouseSource,
                "lever": LeverSource,
                "ashby": AshbySource,
            }[provider](BoardHTTP(client, interval=0))
            return await adapter.fetch_jobs(config(provider, **kwargs))

    return asyncio.run(run())


def test_greenhouse_fixture():
    (job,) = fetch("greenhouse", payload("greenhouse"))
    assert job.source_job_id == "12345"
    assert job.company_identifier == "example"
    assert job.description_text == "Build C++ & Linux systems.\nGraduate applicants welcome."
    assert job.updated_at.hour == 8
    assert job.updated_at.tzinfo == UTC
    assert job.fetched_at == job.first_seen_at == job.last_seen_at
    assert job.fetched_at.tzinfo == UTC
    assert job.posted_at is None
    assert job.remote_type is None
    assert job.eligibility_information == {}
    assert str(job.application_url) == "https://example.org/careers?gh_jid=12345"
    assert job.source_metadata["internal_job_id"] == 67890


def test_lever_fixture():
    (job,) = fetch("lever", payload("lever"))
    assert job.employment_type == "Intern"
    assert job.remote_type == "hybrid"
    assert job.location_text == "Hong Kong"
    assert "Requirements\nPython and Linux\nC++ experience" in job.description_text
    assert "Enrollment requirements vary." in job.description_text
    assert str(job.application_url).endswith("/apply")
    assert job.posted_at is None


def test_ashby_fixture():
    jobs = fetch("ashby", payload("ashby"))
    assert len(jobs) == 2  # unlisted job should be filtered out
    job = jobs[0]
    assert job.source == "ashby"
    assert job.source_job_id == "ashby-123"
    assert job.company_identifier == "example"
    assert job.title == "Senior Software Engineer"
    assert job.location_text == "San Francisco, CA, USA"
    assert job.remote_type == "remote"
    assert job.employment_type == "FullTime"
    assert job.description_text == "Build great things"
    assert str(job.application_url) == "https://jobs.ashbyhq.com/example/ashby-123/apply"
    assert job.source_metadata["compensation_summary"] == "$150k - $200k"
    assert job.source_metadata["department"] == "Engineering"
    assert job.source_metadata["team"] == "Platform"
    assert job.posted_at is not None
    assert job.posted_at.hour == 10
    assert job.posted_at.tzinfo == UTC


@pytest.mark.parametrize(
    "provider,body",
    [
        ("greenhouse", {"jobs": []}),
        ("lever", []),
        ("ashby", {"jobs": []}),
    ],
)
def test_empty_results(provider, body):
    assert fetch(provider, body) == []


@pytest.mark.parametrize(
    "provider,body",
    [
        ("greenhouse", {}),
        ("greenhouse", {"jobs": [{}]}),
        ("lever", {}),
        ("lever", [{}]),
        ("ashby", {}),
        ("ashby", {"jobs": [{}]}),
    ],
)
def test_malformed_response(provider, body):
    with pytest.raises(SourceError, match="Invalid"):
        fetch(provider, body)


@pytest.mark.parametrize("provider", ["greenhouse", "lever", "ashby"])
def test_unsafe_url_rejected(provider):
    body = payload(provider)
    post = body["jobs"][0] if provider in {"greenhouse", "ashby"} else body[0]
    if provider == "greenhouse":
        url_field = "absolute_url"
    elif provider == "ashby":
        url_field = "jobUrl"
    else:
        url_field = "applyUrl"
    post[url_field] = "javascript:alert(1)"
    with pytest.raises(SourceError):
        fetch(provider, body)


def test_optional_fields_are_unknown():
    body = {"jobs": [{"id": 1, "title": "Unusual role", "absolute_url": "https://example.org/job"}]}
    (job,) = fetch("greenhouse", body)
    assert job.description_text == ""
    assert job.location_text is None
    assert job.employment_type is None


def test_naive_source_timestamp_rejected():
    body = payload("greenhouse")
    body["jobs"][0]["updated_at"] = "2026-01-01T10:00:00"
    with pytest.raises(SourceError):
        fetch("greenhouse", body)


def test_greenhouse_request_contract():
    async def run():
        def handler(request):
            assert (
                str(request.url)
                == "https://boards-api.greenhouse.io/v1/boards/example/jobs?content=true"
            )
            assert request.extensions["timeout"]["read"] == 20.0
            return httpx.Response(200, json={"jobs": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await GreenhouseSource(BoardHTTP(client, interval=0)).fetch_jobs(config("greenhouse"))

    asyncio.run(run())


def test_lever_eu_pagination():
    requests = []

    async def run():
        def handler(request):
            requests.append(request)
            assert request.url.host == "api.eu.lever.co"
            assert request.url.params["mode"] == "json"
            assert request.url.params["limit"] == "1"
            return httpx.Response(
                200, json=payload("lever") if request.url.params["skip"] == "0" else []
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await LeverSource(BoardHTTP(client, interval=0), page_size=1).fetch_jobs(
                config("lever", region="eu")
            )

    jobs = asyncio.run(run())
    assert len(jobs) == 1
    assert jobs[0].source_region == "eu"
    assert [r.url.params["skip"] for r in requests] == ["0", "1"]


@pytest.mark.parametrize("max_pages,message", [(2, "repeated"), (1, "limit reached")])
def test_lever_incomplete_pagination_fails(max_pages, message):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload("lever")))
        ) as client:
            return await LeverSource(
                BoardHTTP(client, interval=0), page_size=1, max_pages=max_pages
            ).fetch_jobs(config("lever"))

    with pytest.raises(SourceError, match=message):
        asyncio.run(run())
