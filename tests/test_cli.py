"""Smoke tests for the installed command-line interface."""

import subprocess
import sys
from importlib.metadata import version

import pytest


@pytest.mark.parametrize("command", [["job-scout"], [sys.executable, "-m", "job_scout"]])
def test_help(command: list[str]) -> None:
    result = subprocess.run([*command, "--help"], capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert "usage: job-scout" in result.stdout
    assert "Collection, persistence, and matching" in result.stdout
    assert result.stderr == ""


def test_version() -> None:
    result = subprocess.run(["job-scout", "--version"], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == f"job-scout {version('job-scout')}"


def test_no_arguments_displays_help() -> None:
    result = subprocess.run(["job-scout"], capture_output=True, text=True, check=True)
    assert "usage: job-scout" in result.stdout


def test_unknown_option_fails() -> None:
    result = subprocess.run(["job-scout", "--unknown"], capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr


def test_collect_disabled_examples():
    import json

    result = subprocess.run(
        ["job-scout", "collect", "--config", "config/companies.example.yaml"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {"sources": []}
    assert "No enabled sources" in result.stderr


def test_collect_missing_config(tmp_path):
    result = subprocess.run(
        ["job-scout", "collect", "--config", str(tmp_path / "missing.yaml")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "Cannot load source registry" in result.stderr
    assert "Traceback" not in result.stderr


def test_collect_cli_reports_partial_failure(monkeypatch, capsys):
    import json

    from job_scout import main as cli
    from job_scout.models import (
        CollectionReport,
        SourceConfig,
        SourceRegistry,
        SourceResult,
        utc_now,
    )

    source = SourceConfig(source="lever", company="Example", board_id="example", enabled=True)
    registry = SourceRegistry(sources=[source])
    monkeypatch.setattr(cli, "load_registry", lambda path: registry)

    async def fake_collect(loaded):
        assert loaded == registry
        return CollectionReport(
            sources=[
                SourceResult(
                    config=source, attempted_at=utc_now(), completed_at=utc_now(), error="HTTP 404"
                )
            ]
        )

    monkeypatch.setattr(cli, "_collect", fake_collect)
    assert cli.main(["collect"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["sources"][0]["error"] == "HTTP 404"
