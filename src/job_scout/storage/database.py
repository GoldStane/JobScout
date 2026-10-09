"""Database setup and versioned initial schema for SQLite and PostgreSQL."""

import os
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
)
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

SCHEMA_VERSION = 2
DEFAULT_DATABASE_URL = "sqlite:///data/jobscout.db"


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


class SourceRow(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    region: Mapped[str] = mapped_column(String(16))
    board_id: Mapped[str] = mapped_column(String(200))
    company: Mapped[str]
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
    """Use DATABASE_URL, defaulting to a local SQLite file; never log credentials."""
    url = make_url(database_url or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
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
    """Explicit additive revision 1 -> 2 migration; preserve all jobs and sources."""
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
        if version != 1:
            raise SchemaError("Unsupported schema version; cannot migrate automatically")
        for table in [DigestRow.__table__, NotificationRow.__table__, DeliveryLock.__table__]:
            table.create(connection, checkfirst=True)
        connection.execute(
            SchemaVersion.__table__.update().where(SchemaVersion.id == 1).values(version=2)
        )
