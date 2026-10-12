"""Default cloud targeting must fail closed, never silently switch to SQLite."""

import json

import pytest
from sqlalchemy.engine import make_url

from job_scout import main as cli
from job_scout.storage import database
from job_scout.storage.selection import (
    DatabaseConfigError,
    cloud_database_url,
    database_identity,
    database_selection,
    selected_database_url,
)

CLOUD = "postgresql://user:private-password@db.example.org/jobs?sslmode=require"


def test_missing_database_url_never_creates_local_database(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as error:
        cli.main(["db-init"])
    assert error.value.code == 2
    assert "same PostgreSQL" in capsys.readouterr().err
    assert not (tmp_path / "data").exists()


def test_legacy_sqlite_environment_requires_explicit_flag(tmp_path, monkeypatch, capsys):
    path = tmp_path / "legacy.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    with pytest.raises(SystemExit) as error:
        cli.main(["db-init"])
    assert error.value.code == 2
    assert "--local-db" in capsys.readouterr().err
    assert not path.exists()


@pytest.mark.parametrize("position", ["before", "after", "nested"])
def test_local_flag_overrides_cloud_and_is_scoped(tmp_path, monkeypatch, capsys, position):
    monkeypatch.setenv("DATABASE_URL", CLOUD)
    path = tmp_path / "retained.sqlite"
    args = ["--local-db", str(path)]
    assert cli.main(["db-init", *args]) == 0
    capsys.readouterr()
    command = ["companies", "list"] if position == "nested" else ["db-info"]
    assert cli.main([*args, *command] if position == "before" else [*command, *args]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == [] if position == "nested" else output["backend"] == "sqlite"
    assert selected_database_url().get_backend_name() == "postgresql"
    assert selected_database_url().host == "db.example.org"
    before = path.read_bytes()
    assert cli.main(["jobs", *args]) == 0
    assert path.read_bytes() == before


def test_cloud_failure_never_falls_back_to_sqlite(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", CLOUD)
    seen = []

    def fail(url, **kwargs):
        seen.append(url)
        raise ImportError("private-password")

    monkeypatch.setattr(database, "create_engine", fail)
    with pytest.raises(SystemExit) as error:
        cli.main(["db-init"])
    assert error.value.code == 3
    assert len(seen) == 1 and seen[0].drivername == "postgresql+psycopg"
    assert "private-password" not in capsys.readouterr().err
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize(
    "value",
    [
        "broken:private-password",
        "sqlite:///old.db",
        "postgresql://u:private-password@host/db",
        "postgresql://u:private-password@host/db?sslmode=disable",
    ],
)
def test_cloud_settings_validation_is_credential_safe(value):
    with pytest.raises(DatabaseConfigError) as error:
        cloud_database_url(value)
    assert "private-password" not in str(error.value)


def test_fingerprint_ignores_password_and_driver_alias():
    first = cloud_database_url(CLOUD)
    second = cloud_database_url(
        CLOUD.replace("private-password", "rotated").replace("postgresql://", "postgres://")
    )
    assert database_identity(first) == database_identity(second)
    assert database_identity(first) != database_identity(first.set(database="other"))
    assert "private-password" not in json.dumps(database_identity(first))


def test_selection_restores_after_exception(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", CLOUD)
    with pytest.raises(RuntimeError), database_selection(tmp_path / "test.db"):
        assert selected_database_url().get_backend_name() == "sqlite"
        raise RuntimeError()
    assert selected_database_url() == make_url(CLOUD).set(drivername="postgresql+psycopg")
