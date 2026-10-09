"""Atomic, idempotent source snapshots and persistent job lifecycle."""

from dataclasses import asdict, dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from job_scout.models import CollectionReport, Job, SourceResult
from job_scout.storage.database import JobRow, SourceRow, check_schema


@dataclass
class IngestCounts:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    closed: int = 0
    failed_sources: int = 0
    ignored_snapshots: int = 0


def _validate_snapshot(result: SourceResult) -> None:
    if result.completed_at < result.attempted_at:
        raise ValueError("Collection completion precedes its attempt")
    if result.error is not None and result.jobs:
        raise ValueError("Failed sources must not contain a partial job snapshot")
    seen: set[str] = set()
    config = result.config
    for job in result.jobs:
        if (job.source, job.source_region, job.company_identifier) != (
            config.source,
            config.region,
            config.board_id,
        ):
            raise ValueError("Job identity does not match its source")
        if job.status != "open":
            raise ValueError("Collection snapshots must contain published jobs")
        if not result.attempted_at <= job.fetched_at <= result.completed_at:
            raise ValueError("Job fetch time lies outside the source collection window")
        if job.source_job_id in seen:
            raise ValueError("Duplicate posting ID in one source snapshot")
        seen.add(job.source_job_id)


class Repository:
    def __init__(self, engine: Engine, *, closure_threshold: int = 3) -> None:
        if closure_threshold < 2:
            raise ValueError("Closure requires at least two successful missing checks")
        check_schema(engine)
        self.engine = engine
        self.closure_threshold = closure_threshold

    def ingest(self, report: CollectionReport) -> IngestCounts:
        """Commit each employer independently, retaining successes beside fetch failures."""
        # Validate the entire report before writing anything on programming/input errors.
        for result in report.sources:
            _validate_snapshot(result)
        total = IngestCounts()
        for result in report.sources:
            counts = self._ingest_source(result)
            for key, value in asdict(counts).items():
                setattr(total, key, getattr(total, key) + value)
        return total

    def _ingest_source(self, result: SourceResult) -> IngestCounts:
        counts = IngestCounts()
        config = result.config
        with Session(self.engine) as session, session.begin():
            # Serialize writers for a source. SQLite acquires its write lock on this
            # first statement; PostgreSQL locks the source row below.
            insert = sqlite_insert if self.engine.dialect.name == "sqlite" else postgres_insert
            session.execute(
                insert(SourceRow)
                .values(
                    provider=config.source,
                    region=config.region,
                    board_id=config.board_id,
                    company=config.company,
                    consecutive_failures=0,
                )
                .on_conflict_do_nothing(index_elements=["provider", "region", "board_id"])
            )
            source = session.scalars(
                select(SourceRow)
                .where(
                    SourceRow.provider == config.source,
                    SourceRow.region == config.region,
                    SourceRow.board_id == config.board_id,
                )
                .with_for_update()
            ).one()
            # Replayed reports and late completion of older concurrent runs are no-ops.
            if source.last_attempt is not None and result.attempted_at <= source.last_attempt:
                counts.ignored_snapshots = 1
                return counts
            source.company = config.company
            source.last_attempt = result.attempted_at
            source.last_error = result.error
            if result.error is not None:
                source.consecutive_failures += 1
                counts.failed_sources = 1
                return counts
            source.last_success = result.completed_at
            source.consecutive_failures = 0
            existing = {
                row.source_job_id: row
                for row in session.scalars(select(JobRow).where(JobRow.source_id == source.id))
            }
            for job in result.jobs:
                data = job.model_dump(
                    mode="json", exclude={"first_seen_at", "last_seen_at", "status"}
                )
                row = existing.pop(job.source_job_id, None)
                if row is None:
                    session.add(
                        JobRow(
                            source_id=source.id,
                            source_job_id=job.source_job_id,
                            data=data,
                            first_seen_at=job.fetched_at,
                            last_seen_at=job.fetched_at,
                            last_changed_at=job.fetched_at,
                            status="open",
                            missing_checks=0,
                        )
                    )
                    counts.inserted += 1
                else:
                    previous = {
                        key: value for key, value in row.data.items() if key != "fetched_at"
                    }
                    current = {key: value for key, value in data.items() if key != "fetched_at"}
                    if previous != current or row.status != "open":
                        counts.updated += 1
                        row.last_changed_at = job.fetched_at
                    else:
                        counts.unchanged += 1
                    row.data = data
                    row.last_seen_at = job.fetched_at
                    row.status = "open"
                    row.missing_checks = 0
            for row in existing.values():
                if row.status == "closed":
                    continue
                row.missing_checks += 1
                if row.missing_checks >= self.closure_threshold:
                    row.status = "closed"
                    row.last_changed_at = result.completed_at
                    counts.closed += 1
        return counts

    def jobs(self, *, include_closed: bool = False) -> list[Job]:
        with Session(self.engine) as session:
            statement = select(JobRow).order_by(JobRow.id)
            if not include_closed:
                statement = statement.where(JobRow.status == "open")
            return [
                Job.model_validate(
                    row.data
                    | {
                        "first_seen_at": row.first_seen_at,
                        "last_seen_at": row.last_seen_at,
                        "status": row.status,
                    }
                )
                for row in session.scalars(statement)
            ]

    def source_health(self) -> list[dict]:
        with Session(self.engine) as session:
            return [
                {
                    "source": source.provider,
                    "region": source.region,
                    "board_id": source.board_id,
                    "company": source.company,
                    "last_attempt": source.last_attempt,
                    "last_success": source.last_success,
                    "error": source.last_error,
                    "consecutive_failures": source.consecutive_failures,
                }
                for source in session.scalars(select(SourceRow).order_by(SourceRow.id))
            ]
