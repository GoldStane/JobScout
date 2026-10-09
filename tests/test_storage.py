"""Real SQLite tests for transactional persistence and job lifecycle."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from job_scout.models import CollectionReport, Job, SourceConfig, SourceResult
from job_scout.storage.database import (
    JobRow,
    SchemaError,
    SchemaVersion,
    check_schema,
    initialize_database,
    open_database,
)
from job_scout.storage.repository import Repository

START = datetime(2026, 1, 1, tzinfo=UTC)


def snapshot(day=0, *, ids=("1",), error=None, board="example", title="Graduate Engineer"):
    config = SourceConfig(source="greenhouse", company="Example", board_id=board, enabled=True)
    now = START + timedelta(days=day)
    jobs = [
        Job(
            source="greenhouse",
            source_job_id=identifier,
            company=config.company,
            company_identifier=board,
            title=title,
            location_text="Milan",
            description_text="C++ and Linux",
            application_url=f"https://example.org/{identifier}",
            source_url=f"https://example.org/{identifier}",
            fetched_at=now,
            first_seen_at=now,
            last_seen_at=now,
        )
        for identifier in ids
    ]
    return CollectionReport(
        sources=[
            SourceResult(
                config=config,
                attempted_at=now,
                completed_at=now + timedelta(seconds=1),
                jobs=jobs,
                error=error,
            )
        ]
    )


@pytest.fixture
def database(tmp_path):
    engine = open_database(f"sqlite:///{tmp_path}/test.db")
    initialize_database(engine)
    yield engine
    engine.dispose()


def test_repeated_ingestion_updates_without_duplicates(database):
    repo = Repository(database)
    assert repo.ingest(snapshot()).inserted == 1
    assert repo.ingest(snapshot(1)).unchanged == 1
    assert repo.ingest(snapshot(2, title="C++ Systems Engineer")).updated == 1
    (job,) = repo.jobs()
    assert job.title == "C++ Systems Engineer"
    assert job.first_seen_at == START
    assert job.last_seen_at == START + timedelta(days=2)
    assert job.fetched_at == job.last_seen_at
    assert job.first_seen_at.tzinfo == UTC
    with Session(database) as session:
        row = session.scalars(select(JobRow)).one()
        assert row.last_changed_at == START + timedelta(days=2)


def test_persistence_survives_engine_restart(tmp_path):
    url = f"sqlite:///{tmp_path}/jobs.db"
    first = open_database(url)
    initialize_database(first)
    Repository(first).ingest(snapshot())
    first.dispose()
    second = open_database(url)
    try:
        (job,) = Repository(second).jobs()
        assert job.source_job_id == "1"
        assert job.first_seen_at == START
    finally:
        second.dispose()


def test_missing_requires_successful_checks_and_reopens(database):
    repo = Repository(database)
    repo.ingest(snapshot())
    repo.ingest(snapshot(1, ids=()))
    repo.ingest(snapshot(2, ids=(), error="HTTP 503"))
    repo.ingest(snapshot(3, ids=()))
    assert len(repo.jobs()) == 1
    assert repo.ingest(snapshot(4, ids=())).closed == 1
    assert repo.jobs() == []
    (closed,) = repo.jobs(include_closed=True)
    assert closed.status == "closed"
    assert closed.last_seen_at == START
    assert repo.ingest(snapshot(5)).updated == 1
    (reopened,) = repo.jobs()
    assert reopened.first_seen_at == START
    assert reopened.last_seen_at == START + timedelta(days=5)
    with Session(database) as session:
        assert session.scalars(select(JobRow)).one().missing_checks == 0


def test_reappearance_resets_missing_counter(database):
    repo = Repository(database)
    for day, ids in [(0, ("1",)), (1, ()), (2, ()), (3, ("1",)), (4, ())]:
        repo.ingest(snapshot(day, ids=ids))
    assert len(repo.jobs()) == 1
    with Session(database) as session:
        assert session.scalars(select(JobRow)).one().missing_checks == 1


def test_source_health_retains_last_success_and_recovers(database):
    repo = Repository(database)
    repo.ingest(snapshot())
    (initial,) = repo.source_health()
    repo.ingest(snapshot(1, ids=(), error="HTTP 429"))
    repo.ingest(snapshot(2, ids=(), error="HTTP 503"))
    (failed,) = repo.source_health()
    assert failed["last_success"] == initial["last_success"]
    assert failed["last_attempt"] == START + timedelta(days=2)
    assert failed["consecutive_failures"] == 2
    assert failed["error"] == "HTTP 503"
    repo.ingest(snapshot(3))
    (healthy,) = repo.source_health()
    assert healthy["consecutive_failures"] == 0
    assert healthy["error"] is None
    assert healthy["last_success"] == START + timedelta(days=3, seconds=1)


def test_first_attempt_failure_is_persisted(database):
    repo = Repository(database)
    assert repo.ingest(snapshot(ids=(), error="HTTP 404")).failed_sources == 1
    (source,) = repo.source_health()
    assert source["last_success"] is None
    assert repo.jobs() == []


def test_replays_and_older_results_are_ignored(database):
    repo = Repository(database)
    repo.ingest(snapshot(2))
    assert repo.ingest(snapshot(2)).ignored_snapshots == 1
    assert repo.ingest(snapshot(1, ids=())).ignored_snapshots == 1
    absent = snapshot(3, ids=())
    repo.ingest(absent)
    assert repo.ingest(absent).ignored_snapshots == 1
    with Session(database) as session:
        row = session.scalars(select(JobRow)).one()
        assert row.missing_checks == 1
        assert row.last_seen_at == START + timedelta(days=2)


def test_board_identity_prevents_merging_distinct_openings(database):
    repo = Repository(database)
    repo.ingest(snapshot(board="one"))
    repo.ingest(snapshot(board="two"))
    assert len(repo.jobs()) == 2
    assert len(repo.source_health()) == 2


def test_partial_collection_failure_keeps_successes(database):
    repo = Repository(database)
    report = snapshot(board="success")
    report.sources.extend(snapshot(board="failure", ids=(), error="HTTP 404").sources)
    result = repo.ingest(report)
    assert result.inserted == 1
    assert result.failed_sources == 1
    assert len(repo.jobs()) == 1
    assert len(repo.source_health()) == 2


def test_rejected_snapshot_makes_no_writes(database):
    repo = Repository(database)
    report = snapshot(ids=("1", "1"))
    with pytest.raises(ValueError, match="Duplicate"):
        repo.ingest(report)
    assert repo.jobs() == []
    assert repo.source_health() == []


def test_transaction_rolls_back_health_and_jobs(database, monkeypatch):
    from sqlalchemy import event

    repo = Repository(database)
    repo.ingest(snapshot())

    def reject_insert(mapper, connection, target):
        raise RuntimeError("simulate failed write")

    event.listen(JobRow, "before_insert", reject_insert)
    try:
        with pytest.raises(RuntimeError, match="simulate"):
            repo.ingest(snapshot(1, ids=("1", "2"), title="Changed"))
    finally:
        event.remove(JobRow, "before_insert", reject_insert)
    (job,) = repo.jobs()
    assert job.title == "Graduate Engineer"
    assert job.last_seen_at == START
    assert repo.source_health()[0]["last_attempt"] == START


def test_schema_initialization_is_idempotent(database):
    repo = Repository(database)
    repo.ingest(snapshot())
    initialize_database(database)
    assert len(repo.jobs()) == 1


def test_unknown_schema_version_is_rejected(database):
    with Session(database) as session, session.begin():
        session.get(SchemaVersion, 1).version = 999
    with pytest.raises(SchemaError, match="Unsupported"):
        Repository(database)
    with pytest.raises(SchemaError, match="Unsupported"):
        initialize_database(database)


def test_uninitialized_schema_requires_explicit_setup(tmp_path):
    engine = open_database(f"sqlite:///{tmp_path}/empty.db")
    try:
        with pytest.raises(SchemaError, match="db-init"):
            check_schema(engine)
    finally:
        engine.dispose()


def test_database_url_environment(monkeypatch, tmp_path):
    path = tmp_path / "nested" / "jobs.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    engine = open_database()
    try:
        initialize_database(engine)
        assert path.exists()
    finally:
        engine.dispose()


def test_postgres_schema_compiles_without_a_server():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    from job_scout.storage.database import Base

    for table in Base.metadata.sorted_tables:
        sql = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert "CREATE TABLE" in sql
    assert "TIMESTAMP WITH TIME ZONE" in str(
        CreateTable(JobRow.__table__).compile(dialect=postgresql.dialect())
    )


def test_concurrent_replays_are_serialized(database):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)
    repository = Repository(database)

    def ingest():
        barrier.wait(timeout=5)
        return repository.ingest(snapshot())

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: ingest(), range(2)))
    assert sum(result.inserted for result in results) == 1
    assert sum(result.ignored_snapshots for result in results) == 1
    assert len(repository.jobs()) == 1


def test_job_identity_mismatch_is_rejected(database):
    report = snapshot()
    report.sources[0].jobs[0].company_identifier = "different"
    with pytest.raises(ValueError, match="identity"):
        Repository(database).ingest(report)
    assert Repository(database).source_health() == []


def test_unscheduled_boards_are_not_aged_out(database):
    repository = Repository(database)
    repository.ingest(snapshot(board="paused"))
    for day in range(1, 5):
        repository.ingest(snapshot(day, board="active"))
    assert len(repository.jobs()) == 2
    paused = next(job for job in repository.jobs() if job.company_identifier == "paused")
    assert paused.last_seen_at == START
