import json

import pytest

from job_scout import main as cli
from job_scout.models import CollectionReport, Job, SourceConfig, SourceResult, utc_now
from job_scout.storage.database import initialize_database, open_database
from job_scout.storage.repository import Repository


def test_match_reads_database_without_network_or_profile_storage(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/jobs.db")
    engine = open_database()
    initialize_database(engine)
    now = utc_now()
    source = SourceConfig(source="lever", company="Example", board_id="example", enabled=True)
    posting = Job(
        source="lever",
        company="Example",
        company_identifier="example",
        source_job_id="1",
        title="Graduate C++ Engineer",
        description_text="C++ and Linux",
        application_url="https://example.org/apply",
        source_url="https://example.org/job",
        fetched_at=now,
        first_seen_at=now,
        last_seen_at=now,
    )
    Repository(engine).ingest(
        CollectionReport(
            sources=[
                SourceResult(config=source, attempted_at=now, completed_at=now, jobs=[posting])
            ]
        )
    )
    engine.dispose()
    assert cli.main(["match", "--profile", "config/profile.example.yaml", "--limit", "1"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["total_open_jobs"] == 1
    assert report["matches"][0]["job"]["source_job_id"] == "1"
    assert report["matches"][0]["eligibility"]["status"] == "unknown"
    assert "description_text" not in report["matches"][0]["job"]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--min-score", "nan"],
        ["--min-score", "101"],
        ["--limit", "0"],
        ["--profile", "/nonexistent/profile.yaml"],
        ["--profile", "config/profile.example.yaml", "--track", "missing"],
    ],
)
def test_bad_match_arguments_fail_cleanly(arguments, capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(["match", *arguments])
    assert error.value.code == 2
    assert "Traceback" not in capsys.readouterr().err
