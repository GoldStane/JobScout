"""Offline acceptance for extra coverage, telemetry, scheduling and scoped feedback."""

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import httpx
import pytest
from db_helpers import local_main
from sqlalchemy import select

from job_scout.discovery import jobs_cli
from job_scout.discovery.base import DiscoveryBatch, DiscoveryResult
from job_scout.discovery.providers.remotive import RemotiveProvider
from job_scout.discovery.settings import DiscoverySettings, RemotiveSettings
from job_scout.matching.profile import Profile
from job_scout.models import CollectionReport, utc_now
from job_scout.notifications.digest import job_key
from job_scout.notifications.service import run_digest
from job_scout.sources.base import BoardHTTP, SourceError
from job_scout.storage.database import (
    Base,
    DiscoveryRunRow,
    DiscoveryScheduleRow,
    FeedbackRow,
    SchemaVersion,
    initialize_database,
    migrate_database,
    open_database,
)
from job_scout.storage.delivery import DeliveryRepository, recipient_key
from job_scout.storage.insights import Insights
from job_scout.storage.repository import IngestCounts, Repository

PAYLOAD = {
    "jobs": [
        {
            "id": 1,
            "url": "https://remotive.com/remote-jobs/software-dev/intern-1",
            "company_name": "Example A",
            "title": "Engineering Intern",
            "candidate_required_location": "Hong Kong",
            "job_type": "internship",
            "description": "<p>Python engineering</p><script>hidden()</script>",
            "salary": "$1000 monthly",
            "publication_date": "2026-10-10T10:00:00",
        },
        {
            "id": 2,
            "url": "https://remotive.com/remote-jobs/software-dev/intern-2",
            "company_name": "Example B",
            "title": "Engineering Intern",
            "publication_date": "2026-10-10T12:00:00+02:00",
        },
    ]
}
PROFILE = Profile.model_validate(
    {
        "preferences": {"primary_roles": ["Engineering"]},
        "digest": {"minimum_score": 0},
    }
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/m6.db")
    engine = open_database(os.environ["DATABASE_URL"])
    initialize_database(engine)
    config = tmp_path / "discovery.yaml"
    config.write_text(
        "providers:\n  arbeitnow: {enabled: false}\n  remotive: {enabled: true}\n"
        "registration: {enabled: false}\n"
    )
    profile = tmp_path / "profile.yaml"
    profile.write_text("preferences: {primary_roles: [Engineering]}\ndigest: {minimum_score: 0}\n")
    yield engine, config, profile
    engine.dispose()


def mock(monkeypatch, handler=None):
    calls = []

    def response(request):
        calls.append(request)
        return handler(request) if handler else httpx.Response(200, json=PAYLOAD)

    monkeypatch.setattr(jobs_cli, "PublicAPITransport", lambda **kw: httpx.MockTransport(response))
    return calls


def database_text(engine):
    with engine.connect() as connection:
        return json.dumps(
            {
                name: [dict(r) for r in connection.execute(select(table)).mappings()]
                for name, table in Base.metadata.tables.items()
            },
            default=str,
        )


def parse(payload=PAYLOAD):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
        ) as client:
            return await RemotiveProvider(BoardHTTP(client, attempts=1)).discover_jobs()

    return asyncio.run(run())


def test_remotive_parsing_attribution_and_unknown_timezone():
    page = parse()
    a, b = page.jobs
    assert a.company != b.company and a.application_url is None
    assert a.attribution == "Remotive" and a.attribution_url == a.listing_url
    assert a.location_text == "Hong Kong" and a.remote_type == "remote"
    assert a.salary_text == "$1000 monthly" and "hidden" not in a.description_text
    assert a.posted_at is None and a.source_metadata["publication_date"]
    assert b.posted_at.isoformat() == "2026-10-10T10:00:00+00:00"
    assert b.salary_text is None and b.location_text is None


@pytest.mark.parametrize(
    "url",
    [
        "http://remotive.com/remote-jobs/x",
        "https://evil.test/remote-jobs/x",
        "https://user:pass@remotive.com/remote-jobs/x",
        "https://remotive.com:444/remote-jobs/x",
        "https://127.0.0.1/x",
    ],
)
def test_remotive_unsafe_urls_are_skipped(url):
    page = parse({"jobs": [PAYLOAD["jobs"][0] | {"url": url}, PAYLOAD["jobs"][1]]})
    assert page.skipped == 1 and len(page.jobs) == 1


@pytest.mark.parametrize("payload", [{}, {"jobs": {}}, {"jobs": PAYLOAD["jobs"] * 51}])
def test_remotive_incomplete_or_oversized_payload(payload):
    with pytest.raises(SourceError):
        parse(payload)


def test_empty_feed_and_invalid_item():
    assert not parse({"jobs": []}).jobs
    assert parse({"jobs": [{"id": True}]}).skipped == 1


@pytest.mark.parametrize(
    "kwargs",
    [{"refresh_hours": 5}, {"attempts": 2}, {"max_requests": 2}, {"max_pages": 2}, {"limit": 1001}],
)
def test_remotive_settings_cannot_exceed_allowance(kwargs):
    with pytest.raises(ValueError):
        RemotiveSettings(**kwargs)


def test_empty_registry_cli_cache_metrics_and_feedback(env, monkeypatch, capsys):
    engine, config, profile = env
    calls = mock(monkeypatch)
    args = ["discover-jobs", "--config", str(config), "--profile", str(profile), "--format", "json"]
    assert local_main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ingestion"]["inserted"] == report["matching_jobs"] == 2
    assert (
        "View listing" in report["digest_preview"][0] and "Remotive" in report["digest_preview"][0]
    )
    repo = Repository(engine)
    before = repo.source_health()
    key = report["matches"][0]["job_key"]
    assert local_main(["feedback", "irrelevant", key]) == 0
    capsys.readouterr()
    assert local_main(args) == 0
    cached = json.loads(capsys.readouterr().out)
    assert cached["providers"][0]["status"] == "cached" and cached["requests"] == 0
    assert cached["matching_jobs"] == 1 and repo.source_health() == before
    assert cached["fetched_jobs"] == 0 and cached["cached_jobs"] == 2
    assert "cached jobs; not refreshed" in cached["digest_preview"][0]
    assert len(calls) == 1 and calls[0].url.params["limit"] == "100"
    assert local_main(["discovery-health"]) == 0
    health = json.loads(capsys.readouterr().out)[0]
    assert health["totals"]["requests"] == 1 and health["totals"]["inserted"] == 2
    assert health["totals"]["qualifying_observations"] == 2
    assert health["new_records_per_100_requests"] == 200 and health["next_due"]
    assert local_main(["match", "--profile", str(profile)]) == 0
    assert json.loads(capsys.readouterr().out)["matching_jobs_before_limit"] == 1
    assert local_main(["match", "--profile", str(profile), "--include-dismissed"]) == 0
    assert json.loads(capsys.readouterr().out)["matching_jobs_before_limit"] == 2
    assert local_main(["feedback", "clear", key]) == 0
    capsys.readouterr()
    assert not Insights(engine).feedback()


def test_dry_run_no_requests_or_writes_empty_and_cached(env, monkeypatch, capsys):
    engine, config, profile = env
    calls = mock(monkeypatch)
    args = ["discover-jobs", "--config", str(config), "--profile", str(profile)]
    before = database_text(engine)
    assert local_main(args + ["--dry-run"]) == 0
    assert "Remotive refresh skipped" in capsys.readouterr().out
    assert not calls and before == database_text(engine)
    local_main(args)
    capsys.readouterr()
    before = database_text(engine)
    local_main(args + ["--dry-run"])
    assert "Engineering Intern" in capsys.readouterr().out
    assert len(calls) == 1 and before == database_text(engine)


@pytest.mark.parametrize("failure", [429, 503, "timeout"])
def test_failure_retains_cooldown_and_does_not_retry(env, monkeypatch, failure):
    engine, _, _ = env

    def handler(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(failure)

    calls = mock(monkeypatch, handler)
    config = DiscoverySettings.model_validate({"providers": {"remotive": {"enabled": True}}})
    result, count = asyncio.run(jobs_cli.fetch_remotive(config, engine))
    assert result.error and count == 1
    result, count = asyncio.run(jobs_cli.fetch_remotive(config, engine))
    assert result.status == "cached" and count == 0 and len(calls) == 1


def test_concurrent_refresh_claim_and_expiry(env):
    engine, _, _ = env
    barrier = Barrier(2)
    now = utc_now()

    def claim(_):
        barrier.wait(timeout=5)
        return Insights(engine).claim_refresh("remotive", 6, now=now)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(claim, range(2))) == 1
    assert not Insights(engine).claim_refresh("remotive", 6, now=now + timedelta(hours=5))
    assert Insights(engine).claim_refresh("remotive", 6, now=now + timedelta(hours=6))


def test_metrics_replay_window_and_no_error_content_retention(env):
    engine, _, _ = env
    now = utc_now()
    result = DiscoveryResult(
        provider="arbeitnow",
        attempted_at=now,
        completed_at=now,
        error="PRIVATE_ERROR_SENTINEL",
        requests=1,
    )
    insights = Insights(engine)
    insights.record(result, IngestCounts())
    insights.record(result, IngestCounts())
    health = insights.health(now=now)[0]
    assert health["runs"] == health["failed_or_partial"] == 1
    assert health["last_success"] is None
    assert "PRIVATE_ERROR_SENTINEL" not in database_text(engine)
    assert insights.health(now=now + timedelta(days=31)) == []
    with pytest.raises(ValueError):
        insights.health(366)


def test_feedback_scopes_aliases_positive_does_not_override_eligibility(env):
    engine, _, _ = env
    page = parse()
    repo = Repository(engine)
    now = utc_now()
    repo.ingest_discovery(
        DiscoveryResult(
            provider="remotive",
            attempted_at=page.jobs[0].fetched_at,
            completed_at=now,
            jobs=page.jobs,
        )
    )
    a, b = page.jobs
    insights = Insights(engine)
    insights.set_feedback(job_key(a), "irrelevant", repo)
    assert insights.filter_jobs([b], aliases={job_key(b): [job_key(a), job_key(b)]}) == []
    assert insights.filter_jobs([a], "another-profile") == [a]
    insights.set_feedback(job_key(a), "relevant", repo)
    assert insights.filter_jobs([a]) == [a]
    with pytest.raises(ValueError, match="Unknown job"):
        insights.set_feedback("0" * 64, "irrelevant", repo)
    with pytest.raises(ValueError):
        insights.feedback("invalid scope")


def test_daily_digest_applies_feedback_and_records_metrics(env):
    engine, _, _ = env
    page = parse()
    result = DiscoveryResult(
        provider="remotive",
        attempted_at=page.jobs[0].fetched_at,
        completed_at=utc_now(),
        jobs=page.jobs,
        requests=1,
        pages=1,
    )
    repo = Repository(engine)
    repo.ingest_discovery(result)
    insights = Insights(engine)
    insights.set_feedback(job_key(page.jobs[0]), "irrelevant", repo)
    messages = []

    class Sender:
        async def send(self, text):
            messages.append(text)
            return len(messages)

    async def collect():
        return CollectionReport(sources=[])

    async def discover():
        return DiscoveryBatch(results=[result])

    async def sleep(_):
        pass

    outcome = asyncio.run(
        run_digest(
            repository=repo,
            deliveries=DeliveryRepository(engine),
            profile=PROFILE,
            collect=collect,
            discover=discover,
            chat_id="123",
            sender=Sender(),
            now=utc_now(),
            sleep=sleep,
        )
    )
    assert outcome.status == "sent"
    assert "Example A" not in " ".join(messages) and "Example B" in " ".join(messages)
    assert DeliveryRepository(engine).delivered_keys(recipient_key("123")) == {
        job_key(page.jobs[1])
    }
    assert insights.health()[0]["totals"]["requests"] == 1


def test_revision_five_migration_preserves_data(env):
    engine, _, _ = env
    repo = Repository(engine)
    page = parse()
    repo.ingest_discovery(
        DiscoveryResult(
            provider="remotive",
            attempted_at=page.jobs[0].fetched_at,
            completed_at=utc_now(),
            jobs=page.jobs,
        )
    )
    repo.save_job_links({"a" * 64: "b" * 64})
    with engine.begin() as connection:
        for table in [DiscoveryRunRow, DiscoveryScheduleRow, FeedbackRow]:
            table.__table__.drop(connection)
        connection.execute(SchemaVersion.__table__.update().values(version=5))
    migrate_database(engine)
    migrate_database(engine)
    assert len(repo.jobs()) == 2 and repo.job_links() == {"a" * 64: "b" * 64}
    assert Insights(engine).health() == [] and Insights(engine).feedback() == {}


def test_failing_remotive_does_not_suppress_healthy_feed(env, monkeypatch):
    engine, _, _ = env

    def handler(request):
        if request.url.host == "remotive.com":
            return httpx.Response(503)
        return httpx.Response(200, json={"data": [], "links": {"next": None}})

    mock(monkeypatch, handler)
    config = DiscoverySettings.model_validate({"providers": {"remotive": {"enabled": True}}})
    batch, requests = asyncio.run(jobs_cli.fetch_providers(config, PROFILE, engine=engine))
    assert requests == 2
    healthy, failed = batch.results
    assert healthy.provider == "arbeitnow" and healthy.error is None and healthy.pages == 1
    assert failed.provider == "remotive" and failed.error


def test_feedback_does_not_change_seniority_eligibility(env):
    from job_scout.matching.scoring import rank_jobs

    engine, _, _ = env
    senior = (
        parse()
        .jobs[0]
        .model_copy(update={"title": "Senior Engineering Manager", "employment_type": None})
    )
    result = DiscoveryResult(
        provider="remotive", attempted_at=senior.fetched_at, completed_at=utc_now(), jobs=[senior]
    )
    repo = Repository(engine)
    repo.ingest_discovery(result)
    insights = Insights(engine)
    insights.set_feedback(job_key(senior), "relevant", repo)
    assert rank_jobs(insights.filter_jobs([senior]), PROFILE) == []
