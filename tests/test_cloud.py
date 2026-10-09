"""Cloud configuration handling stays private and deterministic."""

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from job_scout import cloud
from job_scout.matching.profile import Profile


@pytest.fixture
def environment(monkeypatch):
    values = {
        "DATABASE_URL": "postgresql://user:secret@db.example.org/jobs?sslmode=require",
        "TELEGRAM_BOT_TOKEN": "123:private_token",
        "TELEGRAM_CHAT_ID": "123",
        "JOBSCOUT_COMPANIES_YAML": (
            "sources:\n- source: lever\n  board_id: example\n"
            "  company: Private employer\n  enabled: true\n"
        ),
        "JOBSCOUT_PROFILE_YAML": Path("config/profile.example.yaml").read_text(),
        "JOBSCOUT_MODE": "preview",
        "JOBSCOUT_RETRY_UNCERTAIN": "false",
        "JOBSCOUT_ADDITIONAL": "false",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


def test_cloud_materializes_and_cleans_private_files(environment, monkeypatch):
    paths = []

    def invoke(args):
        assert args[0] == "run"
        assert "--dry-run" in args
        for option in ["--config", "--profile"]:
            path = Path(args[args.index(option) + 1])
            paths.append(path)
            assert path.is_file()
            assert path.stat().st_mode & 0o777 == 0o600
        return 0

    monkeypatch.setattr(cloud, "invoke", invoke)
    assert cloud.main() == 0
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///data.db",
        "postgresql://u:p@host/db",
        "not-a-url",
        "postgresql://u:p@host/db?sslmode=disable",
    ],
)
def test_cloud_rejects_ephemeral_or_unencrypted_database(environment, monkeypatch, capsys, url):
    monkeypatch.setenv("DATABASE_URL", url)
    assert cloud.main() == 2
    assert url not in capsys.readouterr().out


@pytest.mark.parametrize(
    "mode,command",
    [("initialize", "db-init"), ("migrate", "db-migrate"), ("status", "delivery-status")],
)
def test_cloud_administrative_modes(environment, monkeypatch, mode, command):
    monkeypatch.setenv("JOBSCOUT_MODE", mode)
    monkeypatch.delenv("JOBSCOUT_PROFILE_YAML")
    calls = []
    monkeypatch.setattr(cloud, "invoke", lambda args: calls.append(args) or 0)
    assert cloud.main() == 0
    assert calls == [[command]]


def test_scheduled_uncertain_retry_is_forbidden(environment, monkeypatch):
    monkeypatch.setenv("JOBSCOUT_MODE", "send")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("JOBSCOUT_RETRY_UNCERTAIN", "true")
    assert cloud.main() == 2


def test_manual_send_can_explicitly_retry_uncertain(environment, monkeypatch):
    monkeypatch.setenv("JOBSCOUT_MODE", "send")
    monkeypatch.setenv("JOBSCOUT_RETRY_UNCERTAIN", "true")
    calls = []
    monkeypatch.setattr(cloud, "invoke", lambda args: calls.append(args) or 0)
    assert cloud.main() == 0
    assert "--retry-uncertain" in calls[0]
    assert "--dry-run" not in calls[0]


@pytest.mark.parametrize(
    "now,due",
    [
        (datetime(2026, 1, 1, 6, 59, tzinfo=UTC), False),
        (datetime(2026, 1, 1, 7, 0, tzinfo=UTC), True),
        (datetime(2026, 7, 1, 5, 59, tzinfo=UTC), False),
        (datetime(2026, 7, 1, 6, 0, tzinfo=UTC), True),
        (datetime(2026, 10, 25, 7, 0, tzinfo=UTC), True),
    ],
)
def test_rome_gate_handles_dst(now, due):
    assert cloud.schedule_due(Profile(), now) is due


def test_schedule_before_morning_does_not_invoke(environment, monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setattr(cloud, "utc_now", lambda: datetime(2026, 1, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(cloud, "invoke", lambda args: pytest.fail("Should not run yet"))
    assert cloud.main() == 0


def test_cloud_suppresses_private_preview_and_errors(monkeypatch, capsys):
    def fake_cli(args):
        print("Private profile text, token, and job descriptions")
        logging.error("private log content")
        print('{"status":"dry_run","source_failures":0}')
        return 0

    monkeypatch.setattr(cloud, "cli_main", fake_cli)
    assert cloud.invoke(["run", "--dry-run"]) == 0
    output = capsys.readouterr()
    assert "Private" not in output.out
    assert "private" not in output.err
    assert '"status": "dry_run"' in output.out

    def failing(args):
        raise RuntimeError("private database URL and password")

    monkeypatch.setattr(cloud, "cli_main", failing)
    assert cloud.invoke(["run"]) == 3
    assert "password" not in capsys.readouterr().out


def test_invalid_yaml_does_not_echo_private_values(environment, monkeypatch, capsys):
    monkeypatch.setenv("JOBSCOUT_PROFILE_YAML", "private_key: private-secret-value")
    assert cloud.main() == 2
    assert "private-secret-value" not in capsys.readouterr().out


def test_workflows_keep_ci_offline_and_schedule_opt_in():
    ci = yaml.safe_load(Path(".github/workflows/test.yml").read_text())
    daily = yaml.safe_load(Path(".github/workflows/daily-search.yml").read_text())
    assert "pull_request" in ci["on"]
    assert "secrets." not in Path(".github/workflows/test.yml").read_text()
    assert daily["permissions"] == {"contents": "read"}
    assert daily["concurrency"]["cancel-in-progress"] is False
    assert "JOBSCOUT_SCHEDULE_ENABLED" in daily["jobs"]["daily"]["if"]
    assert all(item["timezone"] == "Europe/Rome" for item in daily["on"]["schedule"])
    assert daily["on"]["workflow_dispatch"]["inputs"]["mode"]["default"] == "preview"


@pytest.mark.parametrize("mode", ["preview", "send"])
def test_manual_additional_flag(environment, monkeypatch, mode):
    monkeypatch.setenv("JOBSCOUT_MODE", mode)
    monkeypatch.setenv("JOBSCOUT_ADDITIONAL", "true")
    calls = []
    monkeypatch.setattr(cloud, "invoke", lambda args: calls.append(args) or 0)
    assert cloud.main() == 0
    assert "--additional" in calls[0]


@pytest.mark.parametrize("event,mode", [("schedule", "send"), ("workflow_dispatch", "status")])
def test_additional_restricted_to_manual_delivery(environment, monkeypatch, event, mode):
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("JOBSCOUT_MODE", mode)
    monkeypatch.setenv("JOBSCOUT_ADDITIONAL", "true")
    assert cloud.main() == 2
