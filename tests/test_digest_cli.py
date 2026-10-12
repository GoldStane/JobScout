import os

import pytest
from db_helpers import local_main

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
    engine = open_database(os.environ["DATABASE_URL"])
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
        local_main(
            [
                "run",
                "--no-discovery",
                "--dry-run",
                "--config",
                str(registry),
                "--profile",
                "tests/fixtures/profile.yaml",
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
        local_main(["run", "--config", str(registry), "--profile", "tests/fixtures/profile.yaml"])
    assert error.value.code == 2
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err
    assert calls == []


@pytest.mark.parametrize(
    "option,value",
    [("--limit", "0"), ("--limit", "1001"), ("--min-score", "nan"), ("--min-score", "101")],
)
def test_invalid_run_options(option, value):
    with pytest.raises(SystemExit) as error:
        local_main(["run", option, value])
    assert error.value.code == 2


def test_missing_chat_for_status(environment, capsys):
    with pytest.raises(SystemExit) as error:
        local_main(["delivery-status"])
    assert error.value.code == 2
    assert "TELEGRAM_CHAT_ID" in capsys.readouterr().err


def test_monitor_reuses_delivery_without_global_discovery(environment, monkeypatch, capsys):
    engine, registry, calls = environment
    from job_scout.discovery import jobs_cli

    async def fetch(configuration, profile, **kwargs):
        assert not any(
            settings.enabled
            for settings in (
                configuration.providers.arbeitnow,
                configuration.providers.himalayas,
                configuration.providers.remotive,
                configuration.providers.brave,
            )
        )
        return None, 0

    monkeypatch.setattr(jobs_cli, "fetch_providers", fetch)
    assert (
        local_main(
            [
                "monitor",
                "--dry-run",
                "--config",
                str(registry),
                "--profile",
                "tests/fixtures/profile.yaml",
            ]
        )
        == 0
    )
    assert len(calls) == 1
    assert '"status": "dry_run"' in capsys.readouterr().out
    assert Repository(engine).jobs() == []


@pytest.mark.parametrize(
    "previous_status", [None, "sent", "pending", "failed", "uncertain", "sending"]
)
def test_repeated_additional_dry_runs_preserve_every_table(
    environment, monkeypatch, capsys, previous_status
):
    from sqlalchemy import event, select
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session

    from job_scout.notifications.telegram import TelegramSender
    from job_scout.storage.database import Base, DigestRow, NotificationRow

    engine, registry, _ = environment
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    if previous_status:
        now = utc_now()
        with Session(engine) as session, session.begin():
            digest = DigestRow(
                recipient=recipient_key("123"),
                local_date=now.date().isoformat(),
                timezone="UTC",
                created_at=now,
                completed_at=now if previous_status == "sent" else None,
            )
            session.add(digest)
            session.flush()
            session.add(
                NotificationRow(
                    digest_id=digest.id,
                    part_index=0,
                    text="Saved unfinished preview",
                    job_keys=["previous-job-key"],
                    status=previous_status,
                    attempts=0 if previous_status == "pending" else 1,
                    message_id=10 if previous_status == "sent" else None,
                )
            )

    def snapshot():
        with engine.connect() as connection:
            return {
                name: sorted(repr(tuple(row)) for row in connection.execute(select(table)))
                for name, table in Base.metadata.tables.items()
            }

    def forbid_writes(conn, cursor, statement, parameters, context, executemany):
        assert statement.lstrip().split()[0].upper() not in {
            "INSERT",
            "UPDATE",
            "DELETE",
            "REPLACE",
            "CREATE",
            "ALTER",
            "DROP",
        }, "Dry-run attempted a database write"

    async def forbid_send(*args, **kwargs):
        pytest.fail("Dry-run attempted Telegram delivery")

    monkeypatch.setattr(TelegramSender, "send", forbid_send)
    before = snapshot()
    output = []
    event.listen(Engine, "before_cursor_execute", forbid_writes)
    try:
        for _ in range(3):
            assert (
                local_main(
                    [
                        "run",
                        "--no-discovery",
                        "--additional",
                        "--dry-run",
                        "--config",
                        str(registry),
                        "--profile",
                        "tests/fixtures/profile.yaml",
                    ]
                )
                == 0
            )
            output.append(capsys.readouterr().out)
            assert snapshot() == before
    finally:
        event.remove(Engine, "before_cursor_execute", forbid_writes)
    assert output[0] == output[1] == output[2]
    assert "no jobs marked as sent" in output[0]
    assert '"status": "dry_run"' in output[0]
