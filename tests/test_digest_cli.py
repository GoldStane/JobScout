import pytest

from job_scout import main as cli
from job_scout.models import CollectionReport, Job, SourceResult, utc_now
from job_scout.storage.database import initialize_database, open_database
from job_scout.storage.delivery import DeliveryRepository, recipient_key
from job_scout.storage.repository import Repository


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    engine = open_database()
    initialize_database(engine)
    registry = tmp_path / "companies.yaml"
    registry.write_text(
        "sources:\n  - source: lever\n    company: Example\n"
        "    board_id: example\n    enabled: true\n"
    )
    calls = []

    async def fake_collect(loaded):
        calls.append(loaded)
        now = utc_now()
        job = Job(
            source="lever",
            source_job_id="1",
            company="Example",
            company_identifier="example",
            title="Graduate C++ Engineer",
            description_text="Linux Python C++",
            location_text="Italy",
            application_url="https://example.org/apply",
            source_url="https://example.org/job",
            fetched_at=now,
            first_seen_at=now,
            last_seen_at=now,
        )
        return CollectionReport(
            sources=[
                SourceResult(
                    config=loaded.sources[0], attempted_at=now, completed_at=now, jobs=[job]
                )
            ]
        )

    monkeypatch.setattr(cli, "_collect", fake_collect)
    yield engine, registry, calls
    engine.dispose()


def test_cli_dry_run_needs_no_credentials_and_writes_nothing(environment, capsys):
    engine, registry, calls = environment
    assert (
        cli.main(
            [
                "run",
                "--no-discovery",
                "--dry-run",
                "--config",
                str(registry),
                "--profile",
                "config/profile.example.yaml",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "Preview message" in output
    assert "Graduate C++ Engineer" in output
    assert '"status": "dry_run"' in output
    assert len(calls) == 1
    assert Repository(engine).jobs() == []
    assert DeliveryRepository(engine).status(recipient_key("123")) == []


def test_missing_token_fails_before_fetch(environment, capsys):
    _, registry, calls = environment
    with pytest.raises(SystemExit) as error:
        cli.main(["run", "--config", str(registry), "--profile", "config/profile.example.yaml"])
    assert error.value.code == 2
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err
    assert calls == []


@pytest.mark.parametrize(
    "option,value",
    [("--limit", "0"), ("--limit", "1001"), ("--min-score", "nan"), ("--min-score", "101")],
)
def test_invalid_run_options(option, value):
    with pytest.raises(SystemExit) as error:
        cli.main(["run", option, value])
    assert error.value.code == 2


def test_missing_chat_for_status(environment, capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(["delivery-status"])
    assert error.value.code == 2
    assert "TELEGRAM_CHAT_ID" in capsys.readouterr().err
