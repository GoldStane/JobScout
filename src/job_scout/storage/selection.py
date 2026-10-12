"""Cloud-first database selection with a command-scoped, explicit SQLite override."""

import hashlib
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from sqlalchemy.engine import URL, make_url


class DatabaseConfigError(ValueError):
    """Credential-safe database selection error."""


_local_path: ContextVar[Path | None] = ContextVar("jobscout_local_database", default=None)


@contextmanager
def database_selection(local_path: Path | None):
    token = _local_path.set(local_path)
    try:
        yield
    finally:
        _local_path.reset(token)


def cloud_database_url(value: str) -> URL:
    try:
        url = make_url(value)
    except Exception:
        raise DatabaseConfigError(
            "Invalid DATABASE_URL; use a PostgreSQL connection string"
        ) from None
    if url.drivername not in {"postgres", "postgresql", "postgresql+psycopg"}:
        raise DatabaseConfigError(
            "DATABASE_URL must select shared PostgreSQL. For SQLite, explicitly use "
            "--local-db data/jobscout.db (or another file path)."
        )
    if not all([url.host, url.database, url.username, url.password]):
        raise DatabaseConfigError("DATABASE_URL must include host, database, username and password")
    if url.query.get("sslmode") not in {"require", "verify-ca", "verify-full"}:
        raise DatabaseConfigError("DATABASE_URL must enable TLS with sslmode=require or stricter")
    return url.set(drivername="postgresql+psycopg")


def selected_database_url() -> URL:
    local_path = _local_path.get()
    if local_path is not None:
        return URL.create("sqlite", database=str(local_path.expanduser()))
    value = os.environ.get("DATABASE_URL", "").strip()
    if not value:
        raise DatabaseConfigError(
            "Set DATABASE_URL to the same PostgreSQL connection string used by GitHub Actions. "
            "SQLite requires an explicit --local-db data/jobscout.db. "
            "The CLI does not load .env automatically."
        )
    return cloud_database_url(value)


def database_identity(url: URL) -> dict:
    """Compare configured targets without printing connection strings or credentials."""
    backend = url.get_backend_name()
    target = (
        [backend, str(Path(url.database).resolve())]
        if backend == "sqlite"
        else [backend, url.host, url.port or 5432, url.database]
    )
    return {
        "backend": backend,
        "target_fingerprint": hashlib.sha256(json.dumps(target).encode()).hexdigest()[:16],
    }
