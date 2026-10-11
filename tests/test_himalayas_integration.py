"""Himalayas ingestion, delivery, identity and provider-isolation acceptance tests."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from job_scout import main as cli
from job_scout.discovery import jobs_cli
from job_scout.discovery.base import DiscoveryBatch, DiscoveryResult
from job_scout.discovery.registry import Registry
from job_scout.discovery.settings import DiscoverySettings
from job_scout.matching.profile import Profile
from job_scout.models import CollectionReport, Job, SourceConfig, SourceResult, utc_now
from job_scout.notifications.digest import job_key
from job_scout.notifications.service import run_digest
from job_scout.storage.database import CompanyRow, initialize_database, open_database
from job_scout.storage.delivery import DeliveryRepository, recipient_key
from job_scout.storage.repository import Repository

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/jobs.db")
    engine = open_database()
    initialize_database(engine)
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "preferences:\n  primary_roles: [Product Designer, Editor]\ndigest:\n  minimum_score: 0\n"
    )
    configuration = tmp_path / "discovery.yaml"
    configuration.write_text(
        "providers:\n  arbeitnow: {enabled: false}\n  himalayas: {enabled: true}\n"
        "registration: {enabled: false}\n"
    )
    yield engine, profile, configuration
    engine.dispose()


def mock_feed(monkeypatch, payload=None, *, arbeitnow_failure=False):
    himalayas = payload or json.loads((FIXTURES / "himalayas.json").read_text())
    arbeitnow = json.loads((FIXTURES / "arbeitnow.json").read_text())
    hosts = []

    def respond(request):
        hosts.append(request.url.host)
        if request.url.host == "himalayas.app":
            return httpx.Response(200, json=himalayas)
        assert request.url.host == "www.arbeitnow.com"
        return httpx.Response(403 if arbeitnow_failure else 200, json=arbeitnow)

    monkeypatch.setattr(jobs_cli, "PublicAPITransport", lambda **kw: httpx.MockTransport(respond))
    return hosts


def test_himalayas_only_empty_registry_cli_persists_and_previews(environment, monkeypatch, capsys):
    engine, profile, configuration = environment
    mock_feed(monkeypatch)
    args = ["discover-jobs", "--profile", str(profile), "--config", str(configuration)]
    assert cli.main(args + ["--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["provider"] == "himalayas" and report["ingestion"]["inserted"] == 2
    assert report["matching_jobs"] == 2
    assert report["providers"][0]["unique_jobs"] == 2
    assert report["providers"][0]["requests"] == 1
    assert len(Repository(engine).jobs()) == 2 and Registry(engine).active().sources == []
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(CompanyRow)) == 0
    assert cli.main(args) == 0
    text = capsys.readouterr().out
    assert "0 new" in text and "2 unchanged" in text
    assert "Himalayas — https://himalayas.app/" in text
    assert "View listing:" in text and "Apply directly:" not in text
    assert "Reported compensation: 50000–70000 CAD / year" in text
    assert "Timezone restrictions: -8, -5" in text
    assert "2 unique observations" in text and "2 matches" in text


def test_cli_dry_run_writes_nothing(environment, monkeypatch, capsys):
    engine, profile, configuration = environment
    mock_feed(monkeypatch)
    assert (
        cli.main(
            [
                "discover-jobs",
                "--profile",
                str(profile),
                "--config",
                str(configuration),
                "--dry-run",
                "--format",
                "json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["ingestion"] is None and report["matching_jobs"] == 2
    assert Repository(engine).jobs() == []
    assert Repository(engine).source_health() == []
    assert Registry(engine).candidates() == []


def test_daily_cli_himalayas_empty_registry_and_no_discovery(environment, monkeypatch, capsys):
    engine, profile, configuration = environment
    hosts = mock_feed(monkeypatch)
    assert (
        cli.main(
            [
                "run",
                "--profile",
                str(profile),
                "--discovery-config",
                str(configuration),
                "--dry-run",
            ]
        )
        == 0
    )
    text = capsys.readouterr().out
    assert "Product Designer" in text and "Himalayas — https://himalayas.app/" in text
    assert hosts == ["himalayas.app"] and Repository(engine).jobs() == []
    hosts.clear()
    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "run",
                "--profile",
                str(profile),
                "--discovery-config",
                str(configuration),
                "--dry-run",
                "--no-discovery",
            ]
        )
    assert exc.value.code == 4
    capsys.readouterr()
    assert not hosts


def test_failed_provider_keeps_healthy_provider_cli(environment, monkeypatch, capsys):
    engine, profile, configuration = environment
    configuration.write_text(
        "providers:\n  himalayas: {enabled: true}\nregistration: {enabled: false}\n"
    )
    mock_feed(monkeypatch, arbeitnow_failure=True)
    assert (
        cli.main(
            [
                "discover-jobs",
                "--profile",
                str(profile),
                "--config",
                str(configuration),
                "--format",
                "json",
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["ingestion"]["inserted"] == 2 and report["ingestion"]["failed_sources"] == 1
    assert "403" in report["error"] and report["matching_jobs"] == 2
    assert report["providers"][0]["error"] and report["providers"][1]["error"] is None
    assert all(job.source == "himalayas" for job in Repository(engine).jobs())


def test_search_wrapper_uses_selected_profile_and_one_shared_request_budget(monkeypatch):
    requests = []
    payload = json.loads((FIXTURES / "himalayas.json").read_text())

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(jobs_cli, "PublicAPITransport", lambda **kw: httpx.MockTransport(respond))
    settings = DiscoverySettings.model_validate(
        {
            "providers": {
                "arbeitnow": {"enabled": False},
                "himalayas": {
                    "enabled": True,
                    "mode": "search",
                    "max_queries": 3,
                    "max_requests": 1,
                },
            }
        }
    )
    profile = Profile.model_validate(
        {
            "career_tracks": {
                "creative": {
                    "primary_roles": ["editor", "designer", "writer"],
                }
            }
        }
    ).for_track("creative")
    batch, count = asyncio.run(jobs_cli.fetch_providers(settings, profile))
    assert count == 1 and len(requests) == 1
    assert requests[0].url.params["q"] in {"editor", "designer", "writer"}
    assert len(batch.jobs) == 2 and "Request budget" in batch.results[0].error


class Sender:
    def __init__(self):
        self.messages = []

    async def send(self, text):
        self.messages.append(text)
        return len(self.messages)


def digest(engine, batch, collection, *, sender, dry_run=False):
    async def collect():
        return collection

    async def discover():
        return batch

    async def sleep(seconds):
        pass

    profile = Profile.model_validate({"digest": {"minimum_score": 0}})
    return asyncio.run(
        run_digest(
            repository=Repository(engine),
            deliveries=DeliveryRepository(engine),
            profile=profile,
            collect=collect,
            discover=discover,
            chat_id="123",
            sender=sender,
            now=utc_now(),
            dry_run=dry_run,
            sleep=sleep,
        )
    )


def feed_batch(monkeypatch, *, links=False):
    payload = json.loads((FIXTURES / "himalayas.json").read_text())
    if links:
        payload["jobs"][0]["description"] += (
            '<a href="https://jobs.lever.co/example/123">Details</a>'
        )
    mock_feed(monkeypatch, payload)
    settings = DiscoverySettings.model_validate({"providers": {"himalayas": {"enabled": True}}})
    return asyncio.run(jobs_cli.fetch_providers(settings, Profile()))[0]


def test_daily_two_feeds_ats_failure_and_successful_delivery_history(environment, monkeypatch):
    engine, _, _ = environment
    batch = feed_batch(monkeypatch)
    now = utc_now()
    collection = CollectionReport(
        sources=[
            SourceResult(
                config=SourceConfig(source="lever", company="Example", board_id="example"),
                attempted_at=now,
                completed_at=now,
                error="Unavailable",
            )
        ]
    )
    sender = Sender()
    result = digest(engine, batch, collection, sender=sender)
    assert result.status == "sent" and result.source_failures == 1
    text = "\n".join(sender.messages)
    assert "Himalayas" in text and "Arbeitnow" in text and "Unavailable" in text
    assert len(Repository(engine).jobs()) == 4
    assert len(DeliveryRepository(engine).delivered_keys(recipient_key("123"))) == 4
    before = len(sender.messages)
    assert digest(engine, batch, collection, sender=sender).status == "already_sent"
    assert len(sender.messages) == before


def test_both_feeds_deduplicate_with_verified_publisher_and_retain_all_credits(
    environment, monkeypatch
):
    engine, _, _ = environment
    batch = feed_batch(monkeypatch, links=True)
    himalayas_job = batch.results[1].jobs[0]
    # Synthetic second feed occurrence of exactly the same posting.
    arbeitnow_job = (
        batch.results[0]
        .jobs[0]
        .model_copy(
            update={
                "title": himalayas_job.title,
                "source_metadata": {"ats_posting_leads": ["https://jobs.lever.co/example/123"]},
            }
        )
    )
    batch.results[0].jobs = [arbeitnow_job]
    batch.results[1].jobs = [himalayas_job]
    now = utc_now()
    publisher = Job(
        source="lever",
        source_job_id="123",
        company="Example",
        company_identifier="example",
        title=himalayas_job.title,
        source_url="https://jobs.lever.co/example/123",
        application_url="https://jobs.lever.co/example/123/apply",
        fetched_at=now,
        first_seen_at=now,
        last_seen_at=now,
    )
    collection = CollectionReport(
        sources=[
            SourceResult(
                config=SourceConfig(source="lever", company="Example", board_id="example"),
                attempted_at=now,
                completed_at=now,
                jobs=[publisher],
            )
        ]
    )
    sender = Sender()
    assert digest(engine, batch, collection, sender=sender).status == "sent"
    text = "\n".join(sender.messages)
    assert text.count("Product Designer —") == 1
    assert "Source: Himalayas — https://himalayas.app/" in text
    assert "Source: Arbeitnow — https://www.arbeitnow.com/" in text
    assert "Apply directly: https://jobs.lever.co/example/123/apply" in text
    assert DeliveryRepository(engine).delivered_keys(recipient_key("123")) == {
        job_key(arbeitnow_job),
        job_key(himalayas_job),
        job_key(publisher),
    }
    assert len(Repository(engine).job_links()) == 2
    assert "Reported compensation: 50000–70000 CAD / year" in text


def test_failed_himalayas_keeps_healthy_feed_in_dry_run(environment, monkeypatch):
    engine, _, _ = environment
    batch = feed_batch(monkeypatch)
    now = utc_now()
    batch.results[1] = DiscoveryResult(
        provider="himalayas", attempted_at=now, completed_at=now, error="Timeout"
    )
    sender = Sender()
    result = digest(
        engine,
        DiscoveryBatch(results=batch.results),
        CollectionReport(sources=[]),
        sender=sender,
        dry_run=True,
    )
    assert result.source_failures == 1
    assert "Arbeitnow" in "\n".join(result.messages)
    assert "himalayas: Timeout" in "\n".join(result.messages)
    assert not sender.messages and not Repository(engine).jobs()


def test_registration_budget_shared_across_feeds_and_duplicate_boards(environment, monkeypatch):
    engine, _, _ = environment
    batch = feed_batch(monkeypatch)
    for result in batch.results:
        result.jobs[0].source_metadata["ats_board_leads"] = ["https://jobs.lever.co/example"]
    batch.results[1].jobs[1].source_metadata["ats_board_leads"] = [
        "https://jobs.ashbyhq.com/example"
    ]
    payload = json.loads((FIXTURES / "discovery.json").read_text())
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.host == "api.lever.co"
        return httpx.Response(200, json=payload["lever"])

    monkeypatch.setattr(jobs_cli, "PublicAPITransport", lambda **kw: httpx.MockTransport(respond))
    settings = DiscoverySettings.model_validate({"registration": {"max_new_boards_per_run": 1}})
    report = asyncio.run(jobs_cli.register_result(batch, engine, settings, dry_run=False))
    assert report.requests == 1 and report.deferred == 1
    assert len(requests) == len(Registry(engine).active().sources) == 1
    evidence = Registry(engine).candidates()[0]["audit"][0]["evidence"]["postings"]
    assert {entry["provider"] for entry in evidence} == {"arbeitnow", "himalayas"}
