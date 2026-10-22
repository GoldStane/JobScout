"""Real PostgreSQL integration in CI; each test owns a temporary schema."""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from job_scout.models import CollectionReport, Job, SourceConfig, SourceResult
from job_scout.notifications.digest import DigestPart
from job_scout.storage.database import (
    DeliveryLock,
    DigestRow,
    NotificationRow,
    SchemaVersion,
    initialize_database,
    migrate_database,
    open_database,
)
from job_scout.storage.delivery import DeliveryBusy, DeliveryRepository, recipient_key
from job_scout.storage.repository import Repository

pytestmark = pytest.mark.postgres


@pytest.fixture
def postgres():
    value = os.environ.get("TEST_POSTGRES_URL")
    if not value:
        pytest.skip("TEST_POSTGRES_URL not set; PostgreSQL service runs in CI")
    url = make_url(value)
    if url.host not in {"localhost", "127.0.0.1"} or url.database != "jobscout_test":
        pytest.fail("PostgreSQL tests require localhost/jobscout_test, never production")
    schema = "jobscout_test_" + uuid.uuid4().hex
    admin = open_database(value)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    test_url = url.update_query_dict({"options": f"-csearch_path={schema}"})
    engine = open_database(test_url.render_as_string(hide_password=False))
    try:
        initialize_database(engine)
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def snapshot():
    now = datetime(2026, 10, 9, 8, tzinfo=UTC)
    source = SourceConfig(source="lever", company="Example", board_id="example", enabled=True)
    job = Job(
        source="lever",
        source_job_id="1",
        company="Example",
        company_identifier="example",
        title="Engineer",
        application_url="https://example.org/apply",
        source_url="https://example.org/job",
        fetched_at=now,
        first_seen_at=now,
        last_seen_at=now,
    )
    return CollectionReport(
        sources=[SourceResult(config=source, attempted_at=now, completed_at=now, jobs=[job])]
    )


def test_postgres_upsert_and_utc(postgres):
    repo = Repository(postgres)
    assert repo.ingest(snapshot()).inserted == 1
    assert repo.ingest(snapshot()).ignored_snapshots == 1
    (job,) = repo.jobs()
    assert job.first_seen_at.tzinfo == UTC
    assert len(repo.source_health()) == 1


def test_postgres_concurrent_ingestion(postgres):
    repo = Repository(postgres)
    barrier = Barrier(2)

    def ingest(_):
        barrier.wait(timeout=5)
        return repo.ingest(snapshot())

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(ingest, range(2)))
    assert sum(result.inserted for result in results) == 1
    assert len(repo.jobs()) == 1


def test_postgres_outbox_and_lease(postgres):
    deliveries = DeliveryRepository(postgres)
    key = recipient_key("123")
    owner = deliveries.acquire(key)
    with pytest.raises(DeliveryBusy):
        deliveries.acquire(key)
    deliveries.prepare(key, owner, "2026-10-09", "Europe/Rome", [DigestPart("Test", ["job"])])
    part = deliveries.existing(key, "2026-10-09").parts[0]
    deliveries.begin_attempt(key, owner, part.id)
    assert not deliveries.delivered_keys(key)
    deliveries.finish_attempt(key, owner, part.id, message_id=123)
    assert deliveries.delivered_keys(key) == {"job"}
    assert deliveries.existing(key, "2026-10-09").complete
    deliveries.prepare(
        key,
        owner,
        "2026-10-09",
        "Europe/Rome",
        [DigestPart("Additional", ["new-job"])],
        extend_completed=True,
    )
    saved = deliveries.existing(key, "2026-10-09")
    assert not saved.complete
    assert saved.parts[0].status == "sent"
    assert deliveries.delivered_keys(key) == {"job"}
    deliveries.begin_attempt(key, owner, saved.parts[1].id)
    deliveries.finish_attempt(key, owner, saved.parts[1].id, message_id=124)
    assert deliveries.existing(key, "2026-10-09").complete
    assert deliveries.delivered_keys(key) == {"job", "new-job"}
    deliveries.release(key, owner)


def test_postgres_migration_preserves_records(postgres):
    Repository(postgres).ingest(snapshot())
    with postgres.begin() as connection:
        for table in [NotificationRow.__table__, DigestRow.__table__, DeliveryLock.__table__]:
            table.drop(connection)
        connection.execute(SchemaVersion.__table__.update().values(version=1))
    migrate_database(postgres)
    assert len(Repository(postgres).jobs()) == 1
    assert DeliveryRepository(postgres).status(recipient_key("123")) == []


def test_postgres_discovery_registration_and_disabled_seed(postgres):
    from job_scout.discovery.detect import detect
    from job_scout.discovery.registry import Registry
    from job_scout.discovery.validate import Validation
    from job_scout.models import SourceRegistry

    registry = Registry(postgres)
    candidate = detect("https://jobs.lever.co/example")
    saved = registry.record(candidate, Validation("verified", "Verified", True))
    assert registry.record(candidate, Validation("verified", "Verified", True))["id"] == saved["id"]
    registry.disable(saved["source_id"])
    registry.seed(SourceRegistry(sources=[candidate.config.model_copy(update={"enabled": True})]))
    assert registry.active().sources == []
    registry.approve(saved["id"])
    assert len(registry.active().sources) == 1


def test_postgres_actual_revision_two_upgrade(postgres):
    from job_scout.discovery.registry import Registry
    from job_scout.storage.database import CandidateRow, CompanyRow

    Repository(postgres).ingest(snapshot())
    deliveries = DeliveryRepository(postgres)
    key = recipient_key("123")
    owner = deliveries.acquire(key)
    deliveries.prepare(key, owner, "2026-10-09", "Europe/Rome", [DigestPart("Sent", ["old"])])
    part = deliveries.existing(key, "2026-10-09").parts[0]
    deliveries.begin_attempt(key, owner, part.id)
    deliveries.finish_attempt(key, owner, part.id, message_id=123)
    deliveries.release(key, owner)
    with postgres.begin() as connection:
        CandidateRow.__table__.drop(connection)
        for column in [
            "company_id",
            "board_url",
            "registration_origin",
            "registration_status",
            "validated_at",
            "validation_error",
        ]:
            connection.execute(text(f"ALTER TABLE sources DROP COLUMN {column}"))
        CompanyRow.__table__.drop(connection)
        connection.execute(SchemaVersion.__table__.update().values(version=2))
    migrate_database(postgres)
    assert len(Repository(postgres).jobs()) == 1
    assert Registry(postgres).sources()[0]["status"] == "unregistered"
    assert DeliveryRepository(postgres).delivered_keys(key) == {"old"}


def test_postgres_schema_three_upgrade_and_immutable_job_links(postgres):
    from job_scout.storage.database import JobLinkRow

    repo = Repository(postgres)
    repo.ingest(snapshot())
    with postgres.begin() as connection:
        JobLinkRow.__table__.drop(connection)
        connection.execute(SchemaVersion.__table__.update().values(version=3))
    migrate_database(postgres)
    assert len(repo.jobs()) == 1
    repo.save_job_links({"a" * 64: "b" * 64})
    repo.save_job_links({"a" * 64: "c" * 64})
    assert repo.job_links() == {"a" * 64: "b" * 64}


def test_postgres_revision_four_and_concurrent_search_quota(postgres):
    from job_scout.discovery.budgets import SearchBudget
    from job_scout.sources.base import SourceError
    from job_scout.storage.database import DiscoveryBudgetRow

    Repository(postgres).ingest(snapshot())
    with postgres.begin() as connection:
        DiscoveryBudgetRow.__table__.drop(connection)
        connection.execute(SchemaVersion.__table__.update().values(version=4))
    migrate_database(postgres)
    assert len(Repository(postgres).jobs()) == 1
    budget = SearchBudget(postgres, 1, 1)
    barrier = Barrier(2)

    def reserve(_):
        barrier.wait(timeout=5)
        try:
            budget.reserve(datetime(2026, 10, 11, tzinfo=UTC))
            return True
        except SourceError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(reserve, range(2))) == 1


def test_postgres_m6_migration_cooldown_feedback_and_metrics(postgres):
    from job_scout.notifications.digest import job_key
    from job_scout.storage.database import DiscoveryRunRow, DiscoveryScheduleRow, FeedbackRow
    from job_scout.storage.insights import Insights

    repo = Repository(postgres)
    repo.ingest(snapshot())
    with postgres.begin() as connection:
        for table in (DiscoveryRunRow, DiscoveryScheduleRow, FeedbackRow):
            table.__table__.drop(connection)
        connection.execute(SchemaVersion.__table__.update().values(version=5))
    migrate_database(postgres)
    insights = Insights(postgres)
    key = job_key(repo.jobs()[0])
    insights.set_feedback(key, "irrelevant", repo)
    assert insights.filter_jobs(repo.jobs()) == []
    insights.set_feedback(key, "relevant", repo)
    assert len(insights.filter_jobs(repo.jobs())) == 1
    barrier = Barrier(2)

    def claim(_):
        barrier.wait(timeout=5)
        return insights.claim_refresh("remotive", 24)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(claim, range(2))) == 1
    assert insights.health() == []


def test_company_registration_concurrent_boards_share_official_identity(postgres):
    from job_scout.discovery.detect import detect
    from job_scout.discovery.registry import Registry
    from job_scout.discovery.validate import Validation

    barrier = Barrier(2)

    def register(board):
        barrier.wait(timeout=5)
        return Registry(postgres).record(
            detect(f"https://jobs.ashbyhq.com/{board}", name="Example Robotics"),
            Validation("verified", "Synthetic reciprocal ownership evidence", True),
            method="company_search",
            official_website="https://example.org",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(register, ["Example", "ExampleEU"]))
    assert all(result["state"] == "active" for result in results)
    sources = Registry(postgres).sources()
    assert len(sources) == 2
    assert len({source["company_id"] for source in sources}) == 1
    Registry(postgres).disable(sources[0]["id"])
    assert len(Registry(postgres).active().sources) == 1


def test_postgres_validated_seed_import_is_idempotent(postgres):
    import asyncio
    import json
    from pathlib import Path

    import httpx

    from job_scout.discovery.registry import Registry
    from job_scout.discovery.seeds import import_seeds
    from job_scout.models import SourceRegistry

    seeds = SourceRegistry(sources=[snapshot().sources[0].config])
    payload = json.loads(Path("tests/fixtures/discovery.json").read_text())["lever"]

    async def execute():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
        ) as client:
            return await import_seeds(seeds, Registry(postgres), client)

    assert asyncio.run(execute())["counts"] == {"active": 1}
    saved = Registry(postgres).sources()
    assert saved[0]["validated_at"] is not None
    assert saved[0]["origin"] == "manual_seed"
    assert asyncio.run(execute())["requests"] == 0
    assert Registry(postgres).sources() == saved
    Registry(postgres).disable(saved[0]["id"])
    assert asyncio.run(execute())["counts"] == {"preserved": 1}
    assert Registry(postgres).active().sources == []
