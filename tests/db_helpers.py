"""Explicit SQLite selection for offline CLI integration tests."""

import os

from sqlalchemy.engine import make_url

from job_scout import main as cli


def local_main(arguments):
    url = make_url(os.environ.get("DATABASE_URL", "sqlite:///:memory:"))
    assert url.get_backend_name() == "sqlite", "Offline CLI fixtures must never select production"
    return cli.main(["--local-db", url.database, *arguments])
