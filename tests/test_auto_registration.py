"""Offline M2: feed leads -> bounded validation -> registry -> ATS monitoring."""

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest
from db_helpers import local_main
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from job_scout.collection import collect
from job_scout.discovery import jobs_cli
from job_scout.discovery.base import DiscoveredJob, DiscoveryResult
from job_scout.discovery.detect import detect
from job_scout.discovery.registration import ats_links, register_discovered
from job_scout.discovery.registry import Registry
from job_scout.discovery.settings import RegistrationSettings
from job_scout.discovery.validate import Validation
from job_scout.models import utc_now
from job_scout.notifications.digest import job_key
from job_scout.storage.database import NotificationRow, initialize_database, open_database
from job_scout.storage.repository import Repository

FIXTURE = json.loads(Path("tests/fixtures/discovery.json").read_text())


@pytest.fixture
def registry(tmp_path):
    engine = open_database(f"sqlite:///{tmp_path}/m2.db")
    initialize_database(engine)
    yield Registry(engine)
    engine.dispose()


def result(*urls):
    now = utc_now()
    return DiscoveryResult(
        provider="arbeitnow",
        attempted_at=now,
        completed_at=now,
        jobs=[
            DiscoveredJob(
                source_job_id=str(index),
                company="Example",
                title="Graduate Engineer",
                source_url=f"https://www.arbeitnow.com/jobs/{index}",
                fetched_at=now,
                first_seen_at=now,
                last_seen_at=now,
                source_metadata={"ats_board_leads": [url]},
            )
            for index, url in enumerate(urls)
        ],
    )


def register(registry, discovery, handler, *, dry_run=False, **settings):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await register_discovered(
                discovery, registry, client, RegistrationSettings(**settings), dry_run=dry_run
            )

    return asyncio.run(run())


@pytest.mark.parametrize(
    "provider,url",
    [
        ("greenhouse", "https://boards.greenhouse.io/example/jobs/1?utm=x"),
        ("lever", "https://jobs.lever.co/example/1"),
        ("ashby", "https://jobs.ashbyhq.com/example/1"),
    ],
)
def test_register_then_monitor_is_idempotent(registry, provider, url):
    discovery = result(url, url)
    repo = Repository(registry.engine)
    repo.ingest_discovery(discovery)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=FIXTURE[provider])

    report = register(registry, discovery, handler)
    assert len(requests) == 1
    assert report.outcomes[0]["state"] == "active"
    candidate = registry.candidates()[0]
    assert candidate["method"] == "global_discovery"
    assert candidate["validated_at"] is not None
    assert len(candidate["audit"][0]["evidence"]["postings"]) == 2
    assert len(registry.active().sources) == 1
    register(registry, discovery, lambda r: pytest.fail("already registered"))
    assert len(registry.candidates()) == 1

    async def monitor():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await collect(registry.active(), client=client, interval=0)

    first = asyncio.run(monitor())
    assert not first.failed and repo.ingest(first).inserted == 1
    keys = {job_key(job) for job in repo.jobs()}
    assert repo.ingest(asyncio.run(monitor())).inserted == 0
    assert repo.ingest_discovery(discovery).inserted == 0
    assert keys == {job_key(job) for job in repo.jobs()}
    assert len(repo.jobs()) == 3  # Feed identities retained until M3 occurrence reconciliation.
    with Session(registry.engine) as session:
        assert session.scalar(select(func.count()).select_from(NotificationRow)) == 0


def test_html_leads_are_canonical_and_bounded():
    description = (
        '<script><a href="https://jobs.lever.co/hidden">no</a></script>'
        '<a href="https://boards.greenhouse.io/example/jobs/1?secret=x#fragment">job</a>'
        '<a href="https://job-boards.greenhouse.io/example/jobs/2">duplicate</a>'
        '<a href="http://127.0.0.1">private</a>'
        '<a href="https://user:password@jobs.lever.co/secret">bad</a>'
        '<a href="https://careers.smartrecruiters.com/unsupported">no</a>'
        '<a href="https://example.org/careers">unknown</a>'
    )
    assert ats_links(description) == ["https://job-boards.greenhouse.io/example"]
    assert ats_links('&lt;a href="https://jobs.lever.co/example/1"&gt;job&lt;/a&gt;') == [
        "https://jobs.lever.co/example"
    ]
    assert (
        len(
            ats_links(
                "".join(f'<a href="https://jobs.lever.co/board{i}">job</a>' for i in range(30))
            )
        )
        == 10
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "file:///etc/passwd",
        "https://example.org/careers",
        "https://jobs.lever.co.evil.org/example",
        "https://careers.smartrecruiters.com/example",
        "https://169.254.169.254/metadata",
    ],
)
def test_unknown_unsafe_unsupported_never_fetched(registry, url):
    discovery = result(url)
    Repository(registry.engine).ingest_discovery(discovery)
    report = register(registry, discovery, lambda r: pytest.fail("no request permitted"))
    assert report.requests == 0 and registry.candidates() == []
    assert len(Repository(registry.engine).jobs()) == 1


def test_preserve_disabled_and_rejected(registry):
    candidate = detect("https://jobs.lever.co/example")
    registered = registry.record(candidate, Validation("verified", "test", True))
    registry.disable(registered["source_id"])
    discovery = result(candidate.url)
    report = register(registry, discovery, lambda r: pytest.fail("disabled"))
    assert report.outcomes[0]["state"] == "disabled"
    registry.reject(registered["id"])
    report = register(registry, discovery, lambda r: pytest.fail("rejected"))
    assert report.outcomes[0]["state"] == "rejected"
    assert registry.active().sources == []


def test_caps_dry_run_and_no_writes(registry):
    discovery = result("https://jobs.ashbyhq.com/example/1", "https://jobs.lever.co/example/1")
    report = register(
        registry,
        discovery,
        lambda r: httpx.Response(200, json=FIXTURE["ashby"]),
        dry_run=True,
        max_new_boards_per_run=1,
    )
    assert report.requests == 1 and report.deferred == 1
    assert report.outcomes[0]["state"] == "verified"
    assert registry.sources() == [] and registry.candidates() == []
    report = register(
        registry, discovery, lambda r: pytest.fail("zero budget"), max_new_boards_per_run=0
    )
    assert report.requests == 0 and report.deferred == 2


def test_failure_does_not_stop_next_board_or_remove_jobs(registry):
    discovery = result("https://jobs.lever.co/broken/1", "https://jobs.ashbyhq.com/example/1")
    repo = Repository(registry.engine)
    repo.ingest_discovery(discovery)

    def handler(request):
        if request.url.host == "api.lever.co":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1"})
        return httpx.Response(200, json=FIXTURE["ashby"])

    report = register(registry, discovery, handler)
    assert [outcome["state"] for outcome in report.outcomes] == ["pending_validation", "active"]
    assert report.requests == 2
    assert len(repo.jobs()) == 2
    assert all(job.application_url is None for job in repo.jobs())


def test_runtime_and_request_budgets(registry):
    discovery = result("https://jobs.ashbyhq.com/example", "https://jobs.lever.co/example")
    report = register(
        registry, discovery, lambda r: httpx.Response(200, json=FIXTURE["ashby"]), max_requests=1
    )
    assert report.requests == report.deferred == 1

    async def slow(request):
        await asyncio.sleep(1)

    report = register(
        registry, result("https://jobs.lever.co/slow"), slow, max_runtime_seconds=0.01
    )
    assert report.error and report.deferred == 1
    assert any(c["state"] == "pending_validation" for c in registry.candidates())


def test_cli_ingests_before_registration_and_keeps_listing_links(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/cli.db")
    engine = open_database(os.environ["DATABASE_URL"])
    initialize_database(engine)
    payload = json.loads(Path("tests/fixtures/arbeitnow.json").read_text())
    payload["data"][0]["description"] += '<a href="https://jobs.ashbyhq.com/example/1">Apply</a>'

    def handler(request):
        if request.url.host == "www.arbeitnow.com":
            return httpx.Response(200, json=payload)
        assert len(Repository(engine).jobs()) == 2
        return httpx.Response(200, json=FIXTURE["ashby"])

    monkeypatch.setattr(jobs_cli, "PublicAPITransport", lambda **kw: httpx.MockTransport(handler))
    try:
        assert (
            local_main(
                ["discover-jobs", "--format", "json", "--profile", "tests/fixtures/profile.yaml"]
            )
            == 0
        )
        output = json.loads(capsys.readouterr().out)
        assert output["ats_registration"]["outcomes"][0]["state"] == "active"
        assert output["ingestion"]["inserted"] == 2
        assert "View listing:" in "".join(output["digest_preview"])
        assert len(Registry(engine).active().sources) == 1
    finally:
        engine.dispose()
