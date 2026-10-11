"""Offline acceptance: no companies, no employers file, same storage and matcher."""

import asyncio
import copy
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from job_scout import main as cli
from job_scout.discovery import jobs_cli
from job_scout.discovery.base import DiscoveryResult
from job_scout.discovery.http import PublicAPITransport
from job_scout.discovery.orchestrator import discover
from job_scout.discovery.providers.arbeitnow import ENDPOINT, ArbeitnowProvider
from job_scout.discovery.registry import Registry
from job_scout.discovery.settings import ArbeitnowSettings, DiscoverySettings
from job_scout.models import utc_now
from job_scout.notifications.digest import job_key
from job_scout.sources.base import BoardHTTP, SourceError
from job_scout.storage.database import (
    CompanyRow,
    JobRow,
    NotificationRow,
    SourceRow,
    initialize_database,
    open_database,
)
from job_scout.storage.repository import Repository

FIXTURE = Path(__file__).parent / "fixtures/arbeitnow.json"
PROFILE = Path(__file__).resolve().parents[1] / "config/profile.example.yaml"


@pytest.fixture
def payload():
    return json.loads(FIXTURE.read_text())


async def no_sleep(seconds):
    pass


def collect_mock(handler, **settings):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            config = ArbeitnowSettings(**settings)
            http = BoardHTTP(
                client,
                interval=0,
                sleep=no_sleep,
                attempts=config.attempts,
                max_requests=config.max_requests,
                max_bytes=config.max_response_bytes,
            )
            result = await discover(ArbeitnowProvider(http), config)
            return result, http.requests

    return asyncio.run(run())


def test_fixture_normalization_and_listing_identity(payload):
    report, requests = collect_mock(lambda r: httpx.Response(200, json=payload))
    assert requests == report.pages == 1
    assert report.error is None
    first, second = report.jobs
    assert first.application_url is None
    assert first.listing_url == first.source_url
    assert first.attribution == "Arbeitnow"
    assert str(first.attribution_url) == "https://www.arbeitnow.com/"
    assert first.description_text == "Build Linux systems using C++ and Python."
    assert second.description_text == "Develop C++ software on Linux."
    assert first.remote_type == "remote" and second.remote_type is None
    assert first.posted_at.timestamp() == 1791492012
    assert first.posted_at.tzinfo is not None
    assert first.eligibility_information == {}


def test_empty_registry_cli_ingests_matches_and_previews(tmp_path, monkeypatch, capsys, payload):
    monkeypatch.chdir(tmp_path)  # No employers.txt or config/companies.yaml exists here.
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/jobs.db")
    engine = open_database()
    initialize_database(engine)
    try:
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(SourceRow)) == 0
            assert session.scalar(select(func.count()).select_from(CompanyRow)) == 0
        monkeypatch.setattr(
            jobs_cli,
            "PublicAPITransport",
            lambda **kw: httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
        )
        args = ["discover-jobs", "--format", "json", "--profile", str(PROFILE)]
        assert cli.main(args) == 0
        first = json.loads(capsys.readouterr().out)
        assert first["ingestion"]["inserted"] == 2
        assert first["matching_jobs"] == 2
        preview = "\n".join(first["digest_preview"])
        assert "View listing:" in preview and "Apply" not in preview
        assert "Source: Arbeitnow — https://www.arbeitnow.com/" in preview
        repo = Repository(engine)
        original = repo.jobs()
        assert len({job.company for job in original}) == 2
        assert cli.main(args) == 0
        second = json.loads(capsys.readouterr().out)
        assert second["ingestion"]["unchanged"] == 2
        assert second["ingestion"]["inserted"] == 0
        assert [job.first_seen_at for job in repo.jobs()] == [j.first_seen_at for j in original]
        assert [job_key(job) for job in repo.jobs()] == [job_key(j) for j in original]
        assert Registry(engine).active().sources == []
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(CompanyRow)) == 0
            assert session.scalar(select(func.count()).select_from(JobRow)) == 2
            assert session.scalar(select(func.count()).select_from(NotificationRow)) == 0
            feed = session.scalars(select(SourceRow)).one()
            assert feed.registration_status == "feed" and feed.company_id is None
        assert cli.main(["match", "--profile", str(PROFILE)]) == 0
        assert json.loads(capsys.readouterr().out)["total_open_jobs"] == 2
    finally:
        engine.dispose()


def test_dry_run_has_no_writes(tmp_path, monkeypatch, capsys, payload):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/jobs.db")
    engine = open_database()
    initialize_database(engine)
    try:
        monkeypatch.setattr(
            jobs_cli,
            "PublicAPITransport",
            lambda **kw: httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
        )
        assert (
            cli.main(["discover-jobs", "--format", "json", "--profile", str(PROFILE), "--dry-run"])
            == 0
        )
        result = json.loads(capsys.readouterr().out)
        assert result["ingestion"] is None and result["matching_jobs"] == 2
        assert Repository(engine).jobs() == []
        assert Repository(engine).source_health() == []
    finally:
        engine.dispose()


def test_empty_and_incomplete_feed(payload):
    empty, _ = collect_mock(
        lambda r: httpx.Response(200, json={"data": [], "links": {"next": None}})
    )
    assert not empty.jobs and empty.error is None
    payload["data"].append({"title": "Missing identity"})
    payload["data"][1] = {
        k: v for k, v in payload["data"][1].items() if k in {"slug", "title", "company_name", "url"}
    }
    report, _ = collect_mock(lambda r: httpx.Response(200, json=payload))
    assert len(report.jobs) == 2 and report.skipped == 1
    assert report.jobs[1].posted_at is None
    assert report.jobs[1].employment_type is None


@pytest.mark.parametrize("body", [{}, {"data": {}}, {"data": []}, {"data": [], "links": []}])
def test_invalid_envelope_reports_failure(body):
    report, _ = collect_mock(lambda r: httpx.Response(200, json=body))
    assert report.error and not report.jobs


def test_pagination_caps_and_distinct_requisitions(payload):
    visited = []

    def handler(request):
        page = int(request.url.params["page"])
        visited.append(page)
        body = copy.deepcopy(payload)
        for job in body["data"]:
            job["slug"] += f"-{page}"
        body["links"]["next"] = f"{ENDPOINT}?page={page + 1}"
        return httpx.Response(200, json=body)

    result, requests = collect_mock(handler, max_pages=2)
    assert visited == [1, 2] and requests == 2
    assert result.limited and result.error is None
    assert len(result.jobs) == 4  # Identical titles/locations must not collapse requisitions.


def test_duplicates_and_repeated_pages_stop(payload):
    payload["data"].append(payload["data"][0])

    def handler(request):
        payload["links"]["next"] = f"{ENDPOINT}?page={int(request.url.params['page']) + 1}"
        return httpx.Response(200, json=payload)

    report, requests = collect_mock(handler)
    assert requests == 2 and len(report.jobs) == 2
    assert "Repeated" in report.error


@pytest.mark.parametrize(
    "next_url",
    [
        "http://127.0.0.1/private",
        "https://localhost/",
        "https://169.254.169.254/",
        "https://www.arbeitnow.com.evil.test/api/job-board-api?page=2",
        "https://user:secret@www.arbeitnow.com/api/job-board-api?page=2",
        ENDPOINT + "?page=1",
        ENDPOINT + "?page=2&page=3",
        ENDPOINT + "?page=2&key=secret",
        "file:///etc/passwd",
        2,
    ],
)
def test_unsafe_pagination_never_requested(payload, next_url):
    payload["links"]["next"] = next_url
    report, requests = collect_mock(lambda r: httpx.Response(200, json=payload))
    assert report.error and requests == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/jobs/1",
        "https://localhost/jobs/1",
        "file:///tmp/jobs/1",
        "https://user:secret@www.arbeitnow.com/jobs/1",
        "https://www.arbeitnow.com:8080/jobs/1",
    ],
)
def test_unsafe_listing_skipped_without_losing_other_jobs(payload, url):
    payload["data"][0]["url"] = url
    result, _ = collect_mock(lambda r: httpx.Response(200, json=payload))
    assert len(result.jobs) == result.skipped == 1


@pytest.mark.parametrize("kind", ["429", "timeout", "redirect", "oversize"])
def test_transport_failures_are_bounded(kind, payload):
    def handler(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("private detail must not escape", request=request)
        if kind == "redirect":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})
        if kind == "oversize":
            return httpx.Response(200, content=b"x" * 2048)
        return httpx.Response(429, headers={"Retry-After": "0"})

    report, requests = collect_mock(handler, max_response_bytes=1024)
    assert report.error and not report.jobs
    assert requests == (3 if kind in {"429", "timeout"} else 1)
    assert "private detail" not in report.error


def test_request_budget_partial_failure_and_feed_lifecycle(tmp_path, payload):
    payload["links"]["next"] = ENDPOINT + "?page=2"
    result, requests = collect_mock(lambda r: httpx.Response(200, json=payload), max_requests=1)
    assert requests == 1 and len(result.jobs) == 2 and "budget" in result.error
    engine = open_database(f"sqlite:///{tmp_path}/jobs.db")
    initialize_database(engine)
    try:
        repo = Repository(engine)
        assert repo.ingest_discovery(result).inserted == 2
        assert repo.ingest_discovery(result).ignored_snapshots == 1
        for day in range(1, 5):
            now = utc_now() + timedelta(days=day)
            repo.ingest_discovery(
                DiscoveryResult(provider="arbeitnow", attempted_at=now, completed_at=now, pages=1)
            )
        assert len(repo.jobs()) == 2
        with Session(engine) as session:
            assert all(row.missing_checks == 0 for row in session.scalars(select(JobRow)))
    finally:
        engine.dispose()


def test_runtime_deadline():
    async def slow(request):
        await asyncio.sleep(1)

    result, _ = collect_mock(slow, max_runtime_seconds=0.01)
    assert result.error == "Discovery runtime budget exhausted"


@pytest.mark.parametrize(
    "settings",
    [
        {"max_pages": 0},
        {"max_requests": 0},
        {"attempts": 4},
        {"enabled": "yes"},
        {"max_runtime_seconds": float("nan")},
        {"endpoint": "http://localhost"},
    ],
)
def test_invalid_settings_rejected(settings):
    with pytest.raises(ValidationError):
        ArbeitnowSettings(**settings)


def test_unimplemented_providers_rejected():
    with pytest.raises(ValidationError):
        DiscoverySettings.model_validate({"providers": {"unimplemented": {"enabled": True}}})


def test_private_dns_target_rejected():
    async def resolver(host):
        return ["127.0.0.1"]

    async def run():
        transport = PublicAPITransport(
            allowed_hosts={"www.arbeitnow.com"},
            resolver=resolver,
            transport=httpx.MockTransport(lambda r: pytest.fail("must not connect")),
        )
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(SourceError, match="non-public"):
                await client.get(ENDPOINT)

    asyncio.run(run())
