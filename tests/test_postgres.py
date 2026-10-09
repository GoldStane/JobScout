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
