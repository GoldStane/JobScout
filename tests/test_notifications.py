"""Offline end-to-end digest delivery and migration tests."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from job_scout.matching.profile import Profile
from job_scout.matching.scoring import rank_jobs
from job_scout.models import CollectionReport, Job, SourceConfig, SourceResult
from job_scout.notifications.digest import DigestPart, build_digest, job_key, units
from job_scout.notifications.service import run_digest
from job_scout.notifications.telegram import DeliveryError
from job_scout.storage.database import (
    DeliveryLock,
    DigestRow,
    NotificationRow,
    SchemaError,
    SchemaVersion,
    initialize_database,
    migrate_database,
    open_database,
)
from job_scout.storage.delivery import DeliveryBusy, DeliveryRepository, recipient_key
from job_scout.storage.repository import Repository

NOW = datetime(2026, 10, 9, 7, tzinfo=UTC)


def report(count=1, failed=False):
    source = SourceConfig(source="lever", company="Example <&>", board_id="example", enabled=True)
    jobs = [
        Job(
            source="lever",
            source_job_id=str(i),
            company=source.company,
            company_identifier="example",
            title=f"C++ Engineer {i}",
            description_text="C++ and Python",
            location_text="Milan",
            source_url=f"https://example.org/{i}",
            application_url=f"https://example.org/{i}/apply?a=1&b=2",
            fetched_at=NOW,
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
        for i in range(count)
    ]
    return CollectionReport(
        sources=[
            SourceResult(
                config=source,
                attempted_at=NOW,
                completed_at=NOW,
                jobs=[] if failed else jobs,
                error="HTTP 503" if failed else None,
            )
        ]
    )


class FakeSender:
    def __init__(self, fail_at=None, uncertain=False):
        self.messages = []
        self.fail_at = fail_at
        self.uncertain = uncertain

    async def send(self, text):
        self.messages.append(text)
        if len(self.messages) == self.fail_at:
            raise DeliveryError("Simulated delivery failure", uncertain=self.uncertain)
        return len(self.messages)


async def no_sleep(delay):
    pass


@pytest.fixture
def setup(tmp_path):
    engine = open_database(f"sqlite:///{tmp_path}/jobs.db")
    initialize_database(engine)
    profile = Profile()
    profile.digest.minimum_score = 0
    profile.digest.maximum_matches = 50
    yield Repository(engine), DeliveryRepository(engine), profile
    engine.dispose()


def run(
    setup,
    sender,
    *,
    data=None,
    now=NOW,
    dry_run=False,
    retry=False,
    chat_id="123",
    additional=False,
):
    repository, deliveries, profile = setup

    async def collect():
        return data if data is not None else report()

    return asyncio.run(
        run_digest(
            repository=repository,
            deliveries=deliveries,
            profile=profile,
            collect=collect,
            sender=sender,
            chat_id=chat_id,
            now=now,
            dry_run=dry_run,
            retry_uncertain=retry,
            additional=additional,
            sleep=no_sleep,
        )
    )


def test_digest_contains_required_fields_and_literal_markup(setup):
    result = run(setup, None, dry_run=True)
    text = "\n".join(result.messages)
    for expected in [
        "C++ Engineer",
        "Example <&>",
        "Milan",
        "Score:",
        "Type:",
        "Why:",
        "Unknowns:",
        "Apply: https://example.org/0/apply?a=1&b=2",
    ]:
        assert expected in text
    assert all(units(text) <= 4000 for text in result.messages)


def test_dry_run_has_no_database_writes(setup):
    repo, deliveries, _ = setup
    result = run(setup, None, dry_run=True)
    assert result.status == "dry_run"
    assert repo.jobs() == []
    assert repo.source_health() == []
    assert deliveries.existing(recipient_key("123"), "2026-10-09") is None
    assert deliveries.delivered_keys(recipient_key("123")) == set()


def test_success_suppresses_same_day_and_future_job_duplicates(setup):
    sender = FakeSender()
    assert run(setup, sender).status == "sent"
    calls = len(sender.messages)
    assert run(setup, sender).status == "already_sent"
    assert len(sender.messages) == calls
    assert run(setup, sender, now=NOW + timedelta(days=1)).status == "sent"
    assert "No new relevant matches" in sender.messages[-1]
    assert "C++ Engineer" not in sender.messages[-1]


def test_daily_gate_in_profile_timezone(setup):
    sender = FakeSender()
    # 22:30 UTC is the next calendar day in Rome in October.
    run(setup, sender, now=datetime(2026, 10, 9, 22, 30, tzinfo=UTC))
    assert run(setup, sender, now=datetime(2026, 10, 10, 7, tzinfo=UTC)).status == "already_sent"


def test_failed_delivery_retries_without_marking_jobs_sent(setup):
    sender = FakeSender(fail_at=1)
    with pytest.raises(DeliveryError):
        run(setup, sender)
    deliveries = setup[1]
    assert deliveries.delivered_keys(recipient_key("123")) == set()
    assert deliveries.status(recipient_key("123"))[0]["status"] == "failed"
    assert run(setup, FakeSender()).status == "sent"
    assert deliveries.status(recipient_key("123"))[0]["attempts"] == 2


def test_partial_send_resumes_only_unconfirmed_parts(setup):
    sender = FakeSender(fail_at=2)
    with pytest.raises(DeliveryError):
        run(setup, sender, data=report(12))
    assert len(sender.messages) == 2
    deliveries = setup[1]
    assert deliveries.delivered_keys(recipient_key("123"))
    retry = FakeSender()
    assert run(setup, retry).status == "sent"
    assert sender.messages[0] not in retry.messages
    assert retry.messages[0] == sender.messages[1]
    assert len(deliveries.delivered_keys(recipient_key("123"))) == 12


def test_uncertain_delivery_needs_explicit_retry(setup):
    with pytest.raises(DeliveryError):
        run(setup, FakeSender(fail_at=1, uncertain=True))
    retry = FakeSender()
    with pytest.raises(DeliveryError, match="retry-uncertain"):
        run(setup, retry)
    assert retry.messages == []
    assert run(setup, retry, retry=True).status == "sent"


def test_crashed_inflight_attempt_is_uncertain(setup):
    deliveries = setup[1]
    key = recipient_key("123")
    owner = deliveries.acquire(key)
    deliveries.prepare(key, owner, "2026-10-09", "Europe/Rome", [DigestPart("hello", ["job"])])
    saved = deliveries.existing(key, "2026-10-09")
    deliveries.begin_attempt(key, owner, saved.parts[0].id)
    deliveries.release(key, owner)
    with pytest.raises(DeliveryError, match="retry-uncertain"):
        run(setup, FakeSender())
    assert deliveries.delivered_keys(key) == set()


def test_recipient_lease_prevents_concurrent_senders(setup):
    deliveries = setup[1]
    key = recipient_key("123")
    owner = deliveries.acquire(key)
    with pytest.raises(DeliveryBusy):
        deliveries.acquire(key)
    deliveries.release(key, owner)
    second = deliveries.acquire(key)
    assert second != owner
    with pytest.raises(DeliveryBusy):
        deliveries.prepare(key, owner, "2026-10-09", "UTC", [DigestPart("hi", [])])
    deliveries.release(key, second)


def test_partial_source_failure_still_sends_good_jobs(setup):
    data = report()
    failed = report(failed=True).sources[0]
    failed.config = failed.config.model_copy(update={"board_id": "bad"})
    data.sources.append(failed)
    sender = FakeSender()
    result = run(setup, sender, data=data)
    assert result.source_failures == 1
    assert "Reduced coverage" in sender.messages[0]
    assert "C++ Engineer" in "\n".join(sender.messages)


def test_all_failed_is_not_reported_as_no_matches(setup):
    sender = FakeSender()
    run(setup, sender, data=report(failed=True))
    assert "results are unavailable" in sender.messages[0]
    assert "No new relevant matches" not in sender.messages[0]


def test_maximum_matches_leave_overflow_for_later_day(setup):
    setup[2].digest.maximum_matches = 1
    sender = FakeSender()
    run(setup, sender, data=report(2))
    assert len(setup[1].delivered_keys(recipient_key("123"))) == 1
    run(setup, sender, data=report(2), now=NOW + timedelta(days=1))
    assert len(setup[1].delivered_keys(recipient_key("123"))) == 2


def test_minimum_score_filters(setup):
    setup[2].digest.minimum_score = 100
    sender = FakeSender()
    run(setup, sender)
    assert "No new relevant matches" in sender.messages[0]
    assert not setup[1].delivered_keys(recipient_key("123"))


def test_emoji_and_long_fields_split_at_jobs(setup):
    data = report(10)
    for job in data.sources[0].jobs:
        job.title = "🧑" * 500
        job.company = "&<>" * 500
    matches = rank_jobs(data.sources[0].jobs, setup[2])
    parts = build_digest(
        matches,
        now=NOW,
        timezone="Europe/Rome",
        failures=[],
        successful_sources=1,
        maximum_matches=10,
    )
    assert len(parts) > 1
    assert all(units(part.text) <= 4000 for part in parts)
    assert {key for part in parts for key in part.job_keys} == {
        job_key(job) for job in data.sources[0].jobs
    }
    for job in data.sources[0].jobs:
        assert sum(str(job.application_url) in part.text for part in parts) == 1


def test_dry_run_resumes_saved_plan_without_collection(setup):
    with pytest.raises(DeliveryError):
        run(setup, FakeSender(fail_at=1))
    before = setup[1].status(recipient_key("123"))
    preview = run(setup, None, dry_run=True, data=CollectionReport(sources=[]))
    assert preview.messages
    assert setup[1].status(recipient_key("123")) == before


def test_migration_preserves_existing_jobs(setup):
    repo, deliveries, _ = setup
    repo.ingest(report())
    engine = repo.engine
    # Reconstruct revision 1's schema, including a real existing posting.
    with engine.begin() as connection:
        for table in [NotificationRow.__table__, DigestRow.__table__, DeliveryLock.__table__]:
            table.drop(connection)
        connection.execute(SchemaVersion.__table__.update().values(version=1))
    with pytest.raises(SchemaError, match="migration"):
        Repository(engine)
    migrate_database(engine)
    migrate_database(engine)
    assert len(Repository(engine).jobs()) == 1
    assert DeliveryRepository(engine).status(recipient_key("123")) == []
    with Session(engine) as session:
        assert session.scalar(select(SchemaVersion.version)) == 2


def test_additional_digest_keeps_history_and_sends_only_new_jobs(setup):
    sender = FakeSender()
    run(setup, sender)
    original = setup[1].status(recipient_key("123"))[0]
    sender.messages.clear()
    assert run(setup, sender, data=report(2), additional=True).status == "sent"
    text = "\n".join(sender.messages)
    assert "C++ Engineer 1" in text and "C++ Engineer 0" not in text
    assert setup[1].status(recipient_key("123"))[0] == original
    assert len(setup[1].delivered_keys(recipient_key("123"))) == 2
    assert run(setup, sender).status == "already_sent"
    sender.messages.clear()
    run(setup, sender, data=report(2), additional=True)
    assert "No new relevant matches" in sender.messages[0]


def test_additional_preview_is_read_only(setup):
    run(setup, FakeSender())
    before = setup[1].status(recipient_key("123"))
    preview = run(setup, None, data=report(2), dry_run=True, additional=True)
    assert preview.status == "dry_run"
    assert "C++ Engineer 1" in "\n".join(preview.messages)
    assert setup[1].status(recipient_key("123")) == before
    assert len(setup[0].jobs()) == 1


def test_failed_additional_digest_resumes_and_preserves_uncertain_guard(setup):
    run(setup, FakeSender())
    with pytest.raises(DeliveryError):
        run(setup, FakeSender(fail_at=1, uncertain=True), data=report(2), additional=True)
    with pytest.raises(DeliveryError, match="retry-uncertain"):
        run(setup, FakeSender(), additional=True)
    sender = FakeSender()
    assert run(setup, sender, retry=True).status == "sent"
    assert "C++ Engineer 0" not in "\n".join(sender.messages)
    assert len(setup[1].delivered_keys(recipient_key("123"))) == 2
