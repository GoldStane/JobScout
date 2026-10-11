"""Empty-registry acceptance, no search-result retention, and ordinary daily delivery."""

import asyncio
import copy
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from job_scout import main as cli
from job_scout.discovery import jobs_cli
from job_scout.discovery.base import DiscoveryBatch
from job_scout.discovery.detect import detect
from job_scout.discovery.registry import Registry
from job_scout.discovery.settings import DiscoverySettings
from job_scout.discovery.validate import Validation
from job_scout.matching.profile import Profile
from job_scout.models import CollectionReport, utc_now
from job_scout.notifications.digest import job_key
from job_scout.notifications.service import run_digest
from job_scout.storage.database import (
    Base,
    DiscoveryBudgetRow,
    JobRow,
    initialize_database,
    open_database,
)
from job_scout.storage.delivery import DeliveryRepository, recipient_key
from job_scout.storage.repository import Repository

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH = json.loads((FIXTURES / "brave.json").read_text())
PUBLISHERS = json.loads((FIXTURES / "discovery.json").read_text())
PROFILE = Profile.model_validate(
    {"preferences": {"primary_roles": ["Engineer"]}, "digest": {"minimum_score": 0}}
)


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/m5.db")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "FAKE_TEST_KEY")
    engine = open_database()
    initialize_database(engine)
    config = tmp_path / "discovery.yaml"
    config.write_text(
        "providers:\n  arbeitnow: {enabled: false}\n  brave: {enabled: true, max_boards: 1}\n"
    )
    profile = tmp_path / "profile.yaml"
    profile.write_text("preferences:\n  primary_roles: [Engineer]\ndigest: {minimum_score: 0}\n")
    yield engine, config, profile
    engine.dispose()


def mock(monkeypatch, *, search=None, publisher=None):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "api.search.brave.com":
            assert request.headers["X-Subscription-Token"] == "FAKE_TEST_KEY"
            return search(request) if search else httpx.Response(200, json=SEARCH)
        assert "X-Subscription-Token" not in request.headers
        if request.url.host == "www.arbeitnow.com":
            return httpx.Response(200, json=json.loads((FIXTURES / "arbeitnow.json").read_text()))
        if publisher:
            return publisher(request)
        source = {
            "api.lever.co": "lever",
            "boards-api.greenhouse.io": "greenhouse",
            "api.ashbyhq.com": "ashby",
        }[request.url.host]
        return httpx.Response(200, json=PUBLISHERS[source])

    monkeypatch.setattr(jobs_cli, "PublicAPITransport", lambda **kw: httpx.MockTransport(handler))
    return calls


def settings(**brave):
    return DiscoverySettings.model_validate(
        {
            "providers": {
                "arbeitnow": {"enabled": False},
                "brave": {"enabled": True, "max_boards": 1} | brave,
            }
        }
    )


def all_database_text(engine):
    with engine.connect() as connection:
        return json.dumps(
            {
                name: [dict(row) for row in connection.execute(select(table)).mappings()]
                for name, table in Base.metadata.tables.items()
            },
            default=str,
        )


def test_empty_registry_cli_ingests_verified_posting_and_registers_board(
    environment, monkeypatch, capsys
):
    engine, config, profile = environment
    calls = mock(monkeypatch)
    assert (
        cli.main(
            [
                "discover-jobs",
                "--config",
                str(config),
                "--profile",
                str(profile),
                "--country",
                "HK",
                "--format",
                "json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["provider"] == "brave" and report["ingestion"]["inserted"] == 1
    assert report["matching_jobs"] == 1 and report["providers"][0]["publisher_requests"] == 1
    assert report["ats_registration"]["requests"] == 0  # Verified response reused.
    assert len(calls) == 2 and '"Hong Kong"' in calls[0].url.params["q"]
    assert len(Repository(engine).jobs()) == len(Registry(engine).active().sources) == 1
    assert "Apply directly" in "\n".join(report["digest_preview"])
    serialized = all_database_text(engine) + json.dumps(report)
    for sentinel in ["SEARCH_", "FAKE_TEST_KEY", "Hong Kong"]:
        assert sentinel not in serialized  # Neither response data nor query copied to DB/report.


def test_dry_run_skips_metered_http_and_every_database_write(environment, monkeypatch, capsys):
    engine, config, profile = environment
    before = all_database_text(engine)
    monkeypatch.setattr(
        jobs_cli,
        "PublicAPITransport",
        lambda **kw: httpx.MockTransport(
            lambda r: pytest.fail("No metered search or publisher request in dry-run")
        ),
    )
    for command, option in [("discover-jobs", "--config"), ("run", "--discovery-config")]:
        assert cli.main([command, option, str(config), "--profile", str(profile), "--dry-run"]) == 0
        assert "Metered Brave search skipped" in capsys.readouterr().out
    assert all_database_text(engine) == before


def test_disabled_provider_and_missing_key_do_not_make_search_requests(environment, monkeypatch):
    engine, _, _ = environment
    calls = mock(monkeypatch)
    assert asyncio.run(jobs_cli.fetch_brave(DiscoverySettings(), PROFILE, engine)) == (None, 0)
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY")
    result, requests = asyncio.run(jobs_cli.fetch_brave(settings(), PROFILE, engine))
    assert "missing or invalid" in result.error and requests == 0 and not calls
    assert all_database_text(engine).count("brave:day") == 0


def test_cross_run_quota_persists_even_after_failed_network(environment, monkeypatch):
    engine, _, _ = environment
    calls = mock(monkeypatch, search=lambda r: httpx.Response(403, text="private error"))
    config = settings(daily_request_limit=1)
    first, _ = asyncio.run(jobs_cli.fetch_brave(config, PROFILE, engine))
    second, _ = asyncio.run(jobs_cli.fetch_brave(config, PROFILE, engine))
    assert first.requests == 1 and second.requests == 0 and "budget exhausted" in second.error
    assert len(calls) == 1
    assert "private error" not in first.error
    with engine.connect() as connection:
        assert list(connection.execute(select(DiscoveryBudgetRow.requests)).scalars()) == [1, 1]


def test_search_failure_cannot_suppress_free_feed(environment, monkeypatch):
    engine, _, _ = environment
    mock(monkeypatch, search=lambda r: httpx.Response(403))
    config = settings()
    config.providers.arbeitnow.enabled = True
    batch, requests = asyncio.run(jobs_cli.fetch_providers(config, PROFILE, engine=engine))
    assert len(batch.results) == 2 and batch.results[0].error is None
    assert len(batch.jobs) == 2 and batch.results[1].error
    assert requests == 2


@pytest.mark.parametrize("state", ["disabled", "rejected"])
def test_disabled_or_rejected_boards_remain_unfetched(environment, monkeypatch, state):
    engine, _, _ = environment
    registry = Registry(engine)
    candidate = detect("https://jobs.lever.co/example")
    saved = registry.record(candidate, Validation("verified", "Verified", True))
    if state == "disabled":
        registry.disable(saved["source_id"])
    else:
        registry.reject(saved["id"])
    payload = copy.deepcopy(SEARCH)
    payload["web"]["results"] = [payload["web"]["results"][0]]
    calls = mock(monkeypatch, search=lambda r: httpx.Response(200, json=payload))
    result, _ = asyncio.run(jobs_cli.fetch_brave(settings(), PROFILE, engine))
    assert len(calls) == 1 and not result.jobs
    assert not registry.active().sources


def test_unverified_results_never_create_candidates_or_rows(environment, monkeypatch):
    engine, config, profile = environment
    mock(monkeypatch, publisher=lambda r: httpx.Response(200, json=[]))
    assert cli.main(["discover-jobs", "--config", str(config), "--profile", str(profile)]) == 0
    assert not Registry(engine).candidates() and not Registry(engine).sources()
    assert not Repository(engine).jobs()
    assert "SEARCH_" not in all_database_text(engine)


def test_observations_do_not_close_other_jobs_or_advance_board_health(environment, monkeypatch):
    engine, _, _ = environment
    mock(monkeypatch)
    result, _ = asyncio.run(jobs_cli.fetch_brave(settings(), PROFILE, engine))
    source = result.sources[0]
    other = source.jobs[0].model_copy(update={"source_job_id": "other"})
    snapshot = source.model_copy(update={"jobs": [*source.jobs, other]})
    repo = Repository(engine)
    repo.ingest(CollectionReport(sources=[snapshot]))
    before = repo.source_health()
    for day in range(1, 5):
        later = source.completed_at + timedelta(days=day)
        updated = source.model_copy(
            update={
                "attempted_at": later,
                "completed_at": later,
                "jobs": [source.jobs[0].model_copy(update={"fetched_at": later})],
            }
        )
        repo.ingest_observations(updated)
    assert len(repo.jobs()) == 2 and repo.source_health() == before
    with Session(engine) as session:
        assert all(row.missing_checks == 0 for row in session.scalars(select(JobRow)))
    assert repo.ingest_observations(source).ignored_snapshots == 1


class Sender:
    def __init__(self):
        self.messages = []

    async def send(self, text):
        self.messages.append(text)
        return len(self.messages)


def test_daily_delivery_and_later_monitor_share_canonical_identity(environment, monkeypatch):
    engine, _, _ = environment
    mock(monkeypatch)
    result, _ = asyncio.run(jobs_cli.fetch_brave(settings(), PROFILE, engine))
    sender = Sender()

    async def run(day, batch, collection):
        async def collect():
            return collection

        async def discover():
            return batch

        async def sleep(_):
            pass

        return await run_digest(
            repository=Repository(engine),
            deliveries=DeliveryRepository(engine),
            profile=PROFILE,
            collect=collect,
            discover=discover,
            chat_id="123",
            sender=sender,
            now=utc_now() + timedelta(days=day),
            sleep=sleep,
        )

    first = asyncio.run(run(0, DiscoveryBatch(results=[result]), CollectionReport(sources=[])))
    assert first.status == "sent"
    assert any("Engineering Intern" in text for text in sender.messages)
    keys = DeliveryRepository(engine).delivered_keys(recipient_key("123"))
    assert keys == {job_key(result.jobs[0])}
    assert "SEARCH_" not in all_database_text(engine)
    sender.messages.clear()
    asyncio.run(run(1, None, CollectionReport(sources=result.sources)))
    assert all("Engineering Intern" not in text for text in sender.messages)
    assert DeliveryRepository(engine).delivered_keys(recipient_key("123")) == keys


def test_known_employer_name_is_preserved(environment, monkeypatch):
    engine, _, _ = environment
    registry = Registry(engine)
    registry.record(
        detect("https://jobs.lever.co/example", name="Example Publishing"),
        Validation("verified", "Verified", True),
    )
    mock(monkeypatch)
    result, _ = asyncio.run(jobs_cli.fetch_brave(settings(), PROFILE, engine))
    assert result.jobs[0].company == result.sources[0].config.company == "Example Publishing"


def test_explicit_registration_cap_and_disabled_registration(environment, monkeypatch):
    engine, _, _ = environment
    mock(monkeypatch)
    config = settings(max_boards=2)
    config.registration.max_new_boards_per_run = 1
    result, _ = asyncio.run(jobs_cli.fetch_brave(config, PROFILE, engine))
    batch = DiscoveryBatch(results=[result])
    report = asyncio.run(jobs_cli.register_result(batch, engine, config, dry_run=False))
    assert len(registry_sources := Registry(engine).active().sources) == 1
    assert report.requests == 0 and report.deferred == 1
    config.registration.enabled = False
    asyncio.run(jobs_cli.register_result(batch, engine, config, dry_run=False))
    assert Registry(engine).active().sources == registry_sources
