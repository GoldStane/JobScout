"""Text imports reuse validation, preserve operator decisions, and bound requests."""

import asyncio
import json

import httpx
import pytest

from job_scout.discovery import batch
from job_scout.discovery.detect import DiscoveryError
from job_scout.discovery.registry import Registry
from job_scout.main import main
from job_scout.sources.base import BoardHTTP
from job_scout.storage.database import initialize_database, open_database


@pytest.fixture
def registry(tmp_path):
    engine = open_database(f"sqlite:///{tmp_path}/batch.db")
    initialize_database(engine)
    yield Registry(engine)
    engine.dispose()


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch):
    async def pace(self):
        pass

    monkeypatch.setattr(BoardHTTP, "_pace", pace)


def run(entries, registry=None, max_requests=60):
    calls = []

    def handler(request):
        calls.append(request)
        board = request.url.path.split("/")[-1]
        if board == "failed":
            return httpx.Response(429, headers={"Retry-After": "3600"})
        return httpx.Response(
            200,
            json=[
                {
                    "id": "1",
                    "text": "Graduate Engineer",
                    "hostedUrl": f"https://jobs.lever.co/{board}/1",
                    "applyUrl": f"https://jobs.lever.co/{board}/1/apply",
                }
            ],
        )

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await batch.discover(
                entries, "fingerprint", client, registry=registry, max_requests=max_requests
            )

    return asyncio.run(execute()), calls


def test_partial_failure_duplicates_unknown_and_invalid(registry):
    result, calls = run(
        [
            (1, "https://jobs.lever.co/first"),
            (2, "https://jobs.lever.co/first/1?tracking=1"),
            (3, "https://jobs.lever.co/failed"),
            (4, "https://jobs.lever.co/second"),
            (5, "https://careers.example.org/jobs"),
            (6, "https://private:SECRET@jobs.lever.co/third"),
        ],
        registry,
    )
    assert result["counts"] == {
        "active": 2,
        "duplicate": 1,
        "pending_validation": 1,
        "needs_review": 1,
        "invalid": 1,
    }
    assert len(calls) == 3
    assert "SECRET" not in json.dumps(result)
    assert len(registry.active().sources) == 2
    candidates = registry.candidates()
    assert len(candidates) == 4
    assert candidates[0]["method"] == "text_import"
    assert candidates[0]["audit"][0]["evidence"] == {"input_sha256": "fingerprint", "line": 1}


def test_repeat_preserves_disabled_and_rejected_sources(registry):
    entries = [(1, "https://jobs.lever.co/one"), (2, "https://jobs.lever.co/two")]
    run(entries, registry)
    first, second = registry.candidates()
    registry.disable(first["source_id"])
    registry.reject(second["id"])
    result, calls = run(entries, registry)
    assert len(calls) == 1
    assert result["counts"] == {"verified": 1, "rejected": 1}
    assert not registry.active().sources
    assert len(registry.candidates()) == 2
    assert len(registry.sources()) == 2


def test_shared_provider_request_budget(registry):
    result, calls = run(
        [(1, "https://jobs.lever.co/one"), (2, "https://jobs.lever.co/two")],
        registry,
        max_requests=1,
    )
    assert len(calls) == 1
    assert result["counts"] == {"active": 1, "pending_validation": 1}
    assert "budget" in result["results"][1]["reason"]


def test_preview_needs_no_database_and_registers_nothing(tmp_path, monkeypatch, capsys):
    path = tmp_path / "employers.txt"
    path.write_text("# My careers URLs\n\nhttps://careers.example.org/jobs\n")
    monkeypatch.setattr(batch, "open_database", lambda: pytest.fail("No DB in preview"))
    assert main(["discover-sources", "--input", str(path), "--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"]
    assert result["counts"] == {"needs_review": 1}


def test_input_limits_before_database_or_network(tmp_path, monkeypatch):
    path = tmp_path / "employers.txt"
    path.write_text("https://jobs.lever.co/one\nhttps://jobs.lever.co/two\n")
    monkeypatch.setattr(batch, "open_database", lambda: pytest.fail("Input must fail first"))
    with pytest.raises(SystemExit) as error:
        main(["discover-sources", "--input", str(path), "--max-urls", "1"])
    assert error.value.code == 2
    path.write_bytes(b"\xff")
    with pytest.raises(DiscoveryError, match="UTF-8"):
        batch.read_input(path, 100)
    path.write_text("# comments only\n")
    with pytest.raises(DiscoveryError, match="no URLs"):
        batch.read_input(path, 100)
    path.write_bytes(b"x" * (batch.MAX_INPUT_BYTES + 1))
    with pytest.raises(DiscoveryError, match="1 MB"):
        batch.read_input(path, 100)


def test_bom_comments_and_line_numbers(tmp_path):
    path = tmp_path / "employers.txt"
    path.write_text("\ufeff# List\n\n https://jobs.lever.co/example#jobs \n", encoding="utf-8")
    entries, fingerprint = batch.read_input(path, 10)
    assert entries == [(3, "https://jobs.lever.co/example#jobs")]
    assert len(fingerprint) == 64


def test_cli_partial_failure_exit_code(registry, tmp_path, monkeypatch, capsys):
    path = tmp_path / "employers.txt"
    path.write_text("http://localhost/private\nhttps://careers.example.org/jobs\n")
    monkeypatch.setattr(batch, "open_database", lambda: registry.engine)
    assert main(["discover-sources", "--input", str(path)]) == 1
    assert json.loads(capsys.readouterr().out)["counts"] == {"invalid": 1, "needs_review": 1}
    assert len(registry.candidates()) == 1
