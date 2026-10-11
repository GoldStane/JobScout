import json
import subprocess

from job_scout import main as cli
from job_scout.models import CollectionReport, SourceConfig, SourceRegistry, SourceResult, utc_now
from job_scout.storage.database import initialize_database, open_database


def test_database_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/cli.db")
    for command, expected in [
        ("db-init", {"schema_version": 6, "initialized": True}),
        ("jobs", []),
        ("source-health", []),
    ]:
        result = subprocess.run(["job-scout", command], capture_output=True, text=True, check=True)
        assert json.loads(result.stdout) == expected


def test_ingest_stores_failed_source_health(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/cli.db")
    engine = open_database()
    initialize_database(engine)
    engine.dispose()
    source = SourceConfig(source="lever", company="Example", board_id="example", enabled=True)
    monkeypatch.setattr(cli, "load_registry", lambda path: SourceRegistry(sources=[source]))

    async def fake_collect(registry):
        return CollectionReport(
            sources=[
                SourceResult(
                    config=source, attempted_at=utc_now(), completed_at=utc_now(), error="HTTP 503"
                )
            ]
        )

    monkeypatch.setattr(cli, "_collect", fake_collect)
    assert cli.main(["ingest"]) == 1
    assert json.loads(capsys.readouterr().out)["failed_sources"] == 1
    assert cli.main(["source-health"]) == 0
    (health,) = json.loads(capsys.readouterr().out)
    assert health["error"] == "HTTP 503"


def test_database_errors_do_not_leak_credentials(monkeypatch, capsys):
    import pytest
    from sqlalchemy.exc import OperationalError

    def fail():
        raise OperationalError("secret SQL", {}, Exception("password=topsecret"))

    monkeypatch.setattr(cli, "open_database", fail)
    with pytest.raises(SystemExit) as exc:
        cli.main(["jobs"])
    assert exc.value.code == 3
    error = capsys.readouterr().err
    assert "topsecret" not in error
    assert "secret SQL" not in error
    assert "OperationalError" in error
