"""Database setup and versioned initial schema for SQLite and PostgreSQL."""

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from job_scout.storage.selection import selected_database_url

SCHEMA_VERSION = 6


class SchemaError(Exception):
    """Database must be initialized or migrated before use."""


class UTCDateTime(TypeDecorator):
    """SQLite loses timezone offsets; store UTC and restore awareness on reads."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Database timestamps must be timezone-aware")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class SchemaVersion(Base):
    __tablename__ = "schema_version"
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int]
    __table_args__ = (CheckConstraint("id = 1", name="single_schema_version"),)


class CompanyRow(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(primary_key=True)
    identity: Mapped[str] = mapped_column(String(255), unique=True)
    display_name: Mapped[str]
    official_website: Mapped[str | None]
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    discovered_at: Mapped[datetime] = mapped_column(UTCDateTime())


class CandidateRow(Base):
    __tablename__ = "discovery_candidates"
    id: Mapped[int] = mapped_column(primary_key=True)
    identity: Mapped[str] = mapped_column(String(64), unique=True)
    url: Mapped[str]
    config: Mapped[dict | None] = mapped_column(JSON)
    method: Mapped[str] = mapped_column(default="manual_url")
    discovered_at: Mapped[datetime] = mapped_column(UTCDateTime())
    validated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    state: Mapped[str] = mapped_column(String(32), default="discovered")
    reason: Mapped[str]
    endpoint_valid: Mapped[bool] = mapped_column(default=False)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"))
    audit: Mapped[list] = mapped_column(JSON, default=list)


class SourceRow(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    region: Mapped[str] = mapped_column(String(16))
    board_id: Mapped[str] = mapped_column(String(200))
    company: Mapped[str]
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    board_url: Mapped[str | None]
    registration_origin: Mapped[str] = mapped_column(
        String(32), default="legacy", server_default="legacy"
    )
    registration_status: Mapped[str] = mapped_column(
        String(32), default="unregistered", server_default="unregistered"
    )
    validated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    validation_error: Mapped[str | None]
    last_attempt: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_success: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None]
    consecutive_failures: Mapped[int] = mapped_column(default=0)
    __table_args__ = (UniqueConstraint("provider", "region", "board_id", name="uq_source_board"),)


class JobRow(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    source_job_id: Mapped[str] = mapped_column(String(255))
    # Canonical fields and selected source metadata, without duplicated lifecycle fields.
    data: Mapped[dict] = mapped_column(JSON)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime())
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    last_changed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    missing_checks: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (
        UniqueConstraint("source_id", "source_job_id", name="uq_source_posting"),
        CheckConstraint("status IN ('open', 'closed')", name="valid_job_status"),
        CheckConstraint("missing_checks >= 0", name="nonnegative_missing_checks"),
    )


class JobLinkRow(Base):
    """Verified feed-to-publisher equivalence; source records are never destroyed."""

    __tablename__ = "job_links"
    feed_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    publisher_key: Mapped[str] = mapped_column(String(64), index=True)
    verified_at: Mapped[datetime] = mapped_column(UTCDateTime())
    method: Mapped[str] = mapped_column(default="exact_ats_url_and_title")


class DiscoveryBudgetRow(Base):
    """Operational request counts only; never search queries or result data."""

    __tablename__ = "discovery_budgets"
    period: Mapped[str] = mapped_column(String(40), primary_key=True)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (CheckConstraint("requests >= 0", name="nonnegative_requests"),)


class DiscoveryRunRow(Base):
    """Aggregate operational metrics only; no queries, URLs, snippets or profile data."""

    __tablename__ = "discovery_runs"
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    attempted_at: Mapped[datetime] = mapped_column(UTCDateTime(), primary_key=True)
    completed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(24))
    counters: Mapped[dict] = mapped_column(JSON)


class DiscoveryScheduleRow(Base):
    __tablename__ = "discovery_schedule"
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    next_due: Mapped[datetime] = mapped_column(UTCDateTime())


class FeedbackRow(Base):
    __tablename__ = "job_feedback"
    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    rating: Mapped[str] = mapped_column(String(16))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    __table_args__ = (
        CheckConstraint("rating IN ('relevant', 'irrelevant')", name="valid_feedback_rating"),
    )


class DigestRow(Base):
    __tablename__ = "digests"
    id: Mapped[int] = mapped_column(primary_key=True)
    recipient: Mapped[str] = mapped_column(String(64), index=True)
    local_date: Mapped[str] = mapped_column(String(10))
    timezone: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    __table_args__ = (UniqueConstraint("recipient", "local_date", name="uq_daily_digest"),)


class NotificationRow(Base):
    __tablename__ = "notification_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    digest_id: Mapped[int] = mapped_column(ForeignKey("digests.id"), index=True)
    part_index: Mapped[int]
    text: Mapped[str]
    job_keys: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    last_attempt: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None]
    message_id: Mapped[int | None]
    __table_args__ = (
        UniqueConstraint("digest_id", "part_index", name="uq_digest_part"),
        CheckConstraint(
            "status IN ('pending','sending','sent','failed','uncertain')",
            name="valid_delivery_status",
        ),
    )


class DeliveryLock(Base):
    __tablename__ = "delivery_locks"
    recipient: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(36))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())


def open_database(database_url: str | None = None) -> Engine:
    """Use shared PostgreSQL unless SQLite is explicitly selected.

    An explicit URL is retained for internal callers and isolated tests. CLI calls
    use the scoped --local-db override or the validated cloud DATABASE_URL.
    """
    url = make_url(database_url) if database_url is not None else selected_database_url()
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    if url.drivername not in {"sqlite", "sqlite+pysqlite", "postgresql+psycopg"}:
        raise ValueError("Use SQLite or PostgreSQL with the psycopg driver")
    if url.get_backend_name() == "sqlite":
        if url.database and url.database != ":memory:":
            database_path = Path(url.database).expanduser()
            database_path.parent.mkdir(parents=True, exist_ok=True)
            url = url.set(database=str(database_path))
        engine = create_engine(url, connect_args={"timeout": 30}, hide_parameters=True)

        @event.listens_for(engine, "connect")
        def sqlite_connect(connection, record) -> None:
            # Explicit BEGIN provides transactional DDL as well as atomic ingestion.
            connection.isolation_level = None
            connection.execute("PRAGMA foreign_keys=ON")

        @event.listens_for(engine, "begin")
        def sqlite_begin(connection) -> None:
            connection.exec_driver_sql("BEGIN")

        return engine
    return create_engine(url, pool_pre_ping=True, hide_parameters=True)


def _check_schema(connection: Connection) -> None:
    tables = set(inspect(connection).get_table_names())
    if not set(Base.metadata.tables).issubset(tables):
        raise SchemaError(
            "Database needs setup or migration; run db-init for a new database, "
            "db-migrate for an existing one"
        )
    version = connection.execute(
        select(SchemaVersion.version).where(SchemaVersion.id == 1)
    ).scalar()
    if version != SCHEMA_VERSION:
        raise SchemaError("Unsupported schema version; run job-scout db-migrate before continuing")


def check_schema(engine: Engine) -> None:
    with engine.connect() as connection:
        _check_schema(connection)


def initialize_database(engine: Engine) -> None:
    """Create the current schema once. Existing schemas are checked, never silently altered."""
    with engine.begin() as connection:
        tables = set(inspect(connection).get_table_names())
        if tables:
            _check_schema(connection)
            return
        Base.metadata.create_all(connection)
        connection.execute(SchemaVersion.__table__.insert().values(id=1, version=SCHEMA_VERSION))


def migrate_database(engine: Engine) -> None:
    """Additive revisions 1/2/3/4/5 -> 6; preserve jobs, delivery history and source IDs."""
    with engine.begin() as connection:
        tables = set(inspect(connection).get_table_names())
        if not {"schema_version", "sources", "jobs"}.issubset(tables):
            raise SchemaError("Database is not initialized; run job-scout db-init")
        version = connection.execute(
            select(SchemaVersion.version).where(SchemaVersion.id == 1)
        ).scalar()
        if version == SCHEMA_VERSION:
            _check_schema(connection)
            return
        if version not in {1, 2, 3, 4, 5}:
            raise SchemaError("Unsupported schema version; cannot migrate automatically")
        if version == 1:
            for table in [DigestRow.__table__, NotificationRow.__table__, DeliveryLock.__table__]:
                table.create(connection, checkfirst=True)
        CompanyRow.__table__.create(connection, checkfirst=True)
        # Nullable/additive columns work on SQLite and PostgreSQL without rebuilding jobs.
        existing = {column["name"] for column in inspect(connection).get_columns("sources")}
        for name, definition in {
            "company_id": "INTEGER REFERENCES companies(id)",
            "board_url": "TEXT",
            "registration_origin": "VARCHAR(32) NOT NULL DEFAULT 'legacy'",
            "registration_status": "VARCHAR(32) NOT NULL DEFAULT 'unregistered'",
            "validated_at": "TIMESTAMP WITH TIME ZONE"
            if connection.dialect.name == "postgresql"
            else "DATETIME",
            "validation_error": "TEXT",
        }.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE sources ADD COLUMN {name} {definition}"))
        CandidateRow.__table__.create(connection, checkfirst=True)
        JobLinkRow.__table__.create(connection, checkfirst=True)
        DiscoveryBudgetRow.__table__.create(connection, checkfirst=True)
        for table in (DiscoveryRunRow, DiscoveryScheduleRow, FeedbackRow):
            table.__table__.create(connection, checkfirst=True)
        connection.execute(
            SchemaVersion.__table__.update()
            .where(SchemaVersion.id == 1)
            .values(version=SCHEMA_VERSION)
        )
        _check_schema(connection)
