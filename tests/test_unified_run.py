"""M3: offline delivery across independent acquisition paths and persistent aliases."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from job_scout import main as cli
from job_scout.deduplicate import reconcile_jobs
from job_scout.discovery import jobs_cli
from job_scout.discovery.base import DiscoveredJob, DiscoveryResult
from job_scout.discovery.posting_links import canonical_posting_url
from job_scout.discovery.registration import RegistrationReport
from job_scout.matching.profile import Profile
from job_scout.models import CollectionReport, Job, SourceConfig, SourceResult
from job_scout.notifications.digest import job_key
from job_scout.notifications.service import run_digest
from job_scout.notifications.telegram import DeliveryError
from job_scout.storage.database import (
    JobLinkRow,
    SchemaVersion,
    initialize_database,
    migrate_database,
    open_database,
)
from job_scout.storage.delivery import DeliveryRepository, recipient_key
from job_scout.storage.repository import Repository

NOW = datetime(2026, 10, 11, 8, tzinfo=UTC)
PROFILE = Path("config/profile.example.yaml").resolve()


def feed(day=0, *, error=None, linked=True):
    now = NOW + timedelta(days=day)
    job = DiscoveredJob(
        company="Example",
        source_job_id="feed-1",
        title="Engineer",
        source_url="https://www.arbeitnow.com/jobs/example/engineer-1",
        listing_url="https://www.arbeitnow.com/jobs/example/engineer-1",
        attribution="Arbeitnow",
        attribution_url="https://www.arbeitnow.com/",
        fetched_at=now,
        first_seen_at=now,
        last_seen_at=now,
        source_metadata={
            "ats_posting_leads": ["https://jobs.lever.co/example/1"] if linked else []
        },
    )
    return DiscoveryResult(
        provider="arbeitnow",
        attempted_at=now,
        completed_at=now,
        jobs=[] if error else [job],
        pages=0 if error else 1,
        error=error,
    )


def ats(day=0, *, error=None):
    now = NOW + timedelta(days=day)
    config = SourceConfig(source="lever", board_id="example", company="Example", enabled=True)
    job = Job(
        source="lever",
        company="Example",
        company_identifier="example",
        source_job_id="1",
        title="Engineer",
        source_url="https://jobs.lever.co/example/1",
        application_url="https://jobs.lever.co/example/1/apply",
        fetched_at=now,
        first_seen_at=now,
        last_seen_at=now,
    )
    return CollectionReport(
        sources=[
            SourceResult(
                config=config,
                attempted_at=now,
                completed_at=now,
                jobs=[] if error else [job],
                error=error,
            )
        ]
    )


class Sender:
    def __init__(self):
        self.messages = []

    async def send(self, text):
        self.messages.append(text)
        return len(self.messages)


@pytest.fixture
def setup(tmp_path):
    engine = open_database(f"sqlite:///{tmp_path}/m3.db")
    initialize_database(engine)
    yield Repository(engine), DeliveryRepository(engine), Sender()
    engine.dispose()


def run(setup, *, day=0, discovery=None, collection=None, dry_run=False, register=None):
    repo, deliveries, sender = setup

    async def discover():
        return discovery

    async def collect():
        return collection if collection is not None else CollectionReport(sources=[])

    async def sleep(_):
        pass

    profile = Profile()
    profile.digest.minimum_score = 0
    return asyncio.run(
        run_digest(
            repository=repo,
            deliveries=deliveries,
            profile=profile,
            collect=collect,
            discover=discover,
            register=register,
            chat_id="123",
            sender=sender,
            now=NOW + timedelta(days=day),
            dry_run=dry_run,
            sleep=sleep,
        )
    )


def test_same_run_deduplicates_and_records_both_delivery_keys(setup):
    discovery, collection = feed(), ats()
    result = run(setup, discovery=discovery, collection=collection)
    repo, deliveries, sender = setup
    assert result.status == "sent"
    text = "\n".join(sender.messages)
    assert text.count("Engineer — Example") == 1
    assert "Apply directly: https://jobs.lever.co/example/1/apply" in text
    assert "Source: Arbeitnow" in text
    assert len(repo.jobs()) == 2  # Preserve original occurrences/provenance.
    assert repo.job_links() == {job_key(discovery.jobs[0]): job_key(collection.sources[0].jobs[0])}
    assert deliveries.delivered_keys(recipient_key("123")) == {
        job_key(discovery.jobs[0]),
        job_key(collection.sources[0].jobs[0]),
    }


def test_feed_sent_before_publisher_is_not_notified_again(setup):
    run(setup, discovery=feed())
    original_key = job_key(feed().jobs[0])
    assert setup[1].delivered_keys(recipient_key("123")) == {original_key}
    setup[2].messages.clear()
    run(setup, day=1, collection=ats(1))
    assert all("Engineer — Example" not in text for text in setup[2].messages)
    assert original_key in setup[0].job_links()
    # Persisted equivalence survives a later feed response without the original link.
    setup[2].messages.clear()
    run(setup, day=2, discovery=feed(2, linked=False))
    assert all("Engineer — Example" not in text for text in setup[2].messages)


def test_publisher_sent_before_feed_is_not_notified_again(setup):
    run(setup, collection=ats())
    setup[2].messages.clear()
    run(setup, day=1, discovery=feed(1))
    assert all("Engineer — Example" not in text for text in setup[2].messages)


def test_dry_run_deduplicates_but_writes_nothing(setup):
    result = run(setup, discovery=feed(), collection=ats(), dry_run=True)
    assert "\n".join(result.messages).count("Engineer — Example") == 1
    assert setup[0].jobs() == []
    assert setup[0].job_links() == {}
    assert setup[0].source_health() == []
    assert setup[1].status(recipient_key("123")) == []
    assert setup[2].messages == []


@pytest.mark.parametrize("failed", ["feed", "ats", "registration"])
def test_failure_keeps_healthy_jobs_and_discloses_reduced_coverage(setup, failed):
    async def register(_):
        return RegistrationReport(error="validation deadline exceeded")

    result = run(
        setup,
        discovery=feed(error="HTTP 503") if failed == "feed" else feed(),
        collection=ats(error="HTTP 429") if failed == "ats" else ats(),
        register=register if failed == "registration" else None,
    )
    assert result.status == "sent" and result.source_failures == 1
    assert "Reduced coverage" in "\n".join(setup[2].messages)
    assert "Engineer — Example" in "\n".join(setup[2].messages)


def test_partial_feed_pages_are_still_delivered(setup):
    discovery = feed()
    discovery.error = "HTTP 503 on later page"
    result = run(setup, discovery=discovery)
    assert result.source_failures == 1
    assert len(setup[0].jobs()) == 1
    assert "View listing:" in "\n".join(setup[2].messages)


def test_titles_or_boards_alone_never_merge_and_ambiguous_links_remain_separate():
    publisher = ats().sources[0].jobs[0]
    for mode in ["no_link", "wrong_title", "wrong_id", "ambiguous"]:
        job = feed(linked=mode != "no_link").jobs[0]
        if mode == "wrong_title":
            job.title = "Different role"
        if mode == "wrong_id":
            job.source_metadata["ats_posting_leads"] = ["https://jobs.lever.co/example/2"]
        if mode == "ambiguous":
            job.source_metadata["ats_posting_leads"].append("https://jobs.lever.co/example/2")
        reconciled = reconcile_jobs([job, publisher], [], {})
        assert len(reconciled.jobs) == 2 and reconciled.new_links == {}


def test_identical_titles_on_distinct_requisitions_remain_separate():
    first = ats().sources[0].jobs[0]
    second = first.model_copy(
        update={"source_job_id": "2", "source_url": "https://jobs.lever.co/example/2"}
    )
    reconciled = reconcile_jobs([feed().jobs[0], first, second], [], {})
    assert len(reconciled.jobs) == 2
    assert len(reconciled.new_links) == 1


def test_never_substitute_stale_publisher_application_link():
    job, publisher = feed().jobs[0], ats().sources[0].jobs[0]
    reconciled = reconcile_jobs([job], [publisher], {})
    assert len(reconciled.jobs) == 1 and reconciled.jobs[0].application_url is None
    assert len(reconciled.aliases[job_key(job)]) == 2


def test_uncertain_unified_digest_resumes_without_refetch(setup):
    class Uncertain:
        async def send(self, text):
            raise DeliveryError("uncertain", uncertain=True)

    repo, deliveries, sender = setup
    with pytest.raises(DeliveryError):
        run((repo, deliveries, Uncertain()), discovery=feed(), collection=ats())
    assert deliveries.delivered_keys(recipient_key("123")) == set()

    async def forbidden():
        pytest.fail("Existing outbox must resume without new acquisition")

    async def no_sleep(_):
        pass

    result = asyncio.run(
        run_digest(
            repository=repo,
            deliveries=deliveries,
            profile=Profile(),
            collect=forbidden,
            discover=forbidden,
            chat_id="123",
            sender=sender,
            now=NOW,
            retry_uncertain=True,
            sleep=no_sleep,
        )
    )
    assert result.status == "sent" and len(sender.messages) == 1
    assert len(deliveries.delivered_keys(recipient_key("123"))) == 2


def test_actual_schema_three_upgrade_preserves_jobs_and_sent_history(setup):
    repo, deliveries, _ = setup
    run(setup, discovery=feed())
    keys = deliveries.delivered_keys(recipient_key("123"))
    with repo.engine.begin() as connection:
        JobLinkRow.__table__.drop(connection)
        connection.execute(SchemaVersion.__table__.update().values(version=3))
    migrate_database(repo.engine)
    migrate_database(repo.engine)
    assert len(repo.jobs()) == 1 and repo.job_links() == {}
    assert deliveries.delivered_keys(recipient_key("123")) == keys
    with Session(repo.engine) as session:
        assert session.scalar(select(SchemaVersion.version)) == 6


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://boards.greenhouse.io/example/jobs/123?utm=x#top",
            "https://job-boards.greenhouse.io/example/jobs/123",
        ),
        ("https://jobs.lever.co/example/1/apply?tracking=x", "https://jobs.lever.co/example/1"),
        ("https://jobs.ashbyhq.com/example/1/application", "https://jobs.ashbyhq.com/example/1"),
        ("https://jobs.lever.co/example", None),
        ("https://user:secret@jobs.lever.co/example/1", None),
        ("http://127.0.0.1/example/1", None),
        ("https://jobs.lever.co/example/1/other", None),
    ],
)
def test_posting_url_identity(url, expected):
    assert canonical_posting_url(url) == expected


def test_run_cli_empty_registry_without_company_files(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/jobs.db")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    engine = open_database()
    initialize_database(engine)

    async def fetch(configuration):
        return feed(), 1

    monkeypatch.setattr(jobs_cli, "fetch_discovery", fetch)
    try:
        assert cli.main(["run", "--dry-run", "--profile", str(PROFILE), "--min-score", "0"]) == 0
        assert "Engineer — Example" in capsys.readouterr().out
        assert Repository(engine).jobs() == []
    finally:
        engine.dispose()


def test_readable_discovery_default_and_explicit_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/jobs.db")
    engine = open_database()
    initialize_database(engine)
    payload = json.loads(Path("tests/fixtures/arbeitnow.json").read_text())
    payload["data"][0]["location"] = "Zürich"
    monkeypatch.setattr(
        jobs_cli,
        "PublicAPITransport",
        lambda **kw: httpx.MockTransport(lambda r: httpx.Response(200, json=payload)),
    )
    args = ["discover-jobs", "--profile", str(PROFILE), "--limit", "1"]
    try:
        assert cli.main(args) == 0
        output = capsys.readouterr().out
        assert "JobScout · Job discovery" in output and "showing 1 of 2" in output
        assert "Zürich" in output and "\\u00" not in output and "\\n" not in output
        assert "Storage: 2 new" in output and "View listing:" in output
        assert "previously undelivered" not in output
        assert cli.main([*args, "--format", "json"]) == 0
        output = capsys.readouterr().out
        data = json.loads(output)
        assert len(data["matches"]) == 1 and "Zürich" in output
        assert data["ingestion"]["unchanged"] == 2
        assert all("previously undelivered" not in part for part in data["digest_preview"])
    finally:
        engine.dispose()
