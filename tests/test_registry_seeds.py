"""M7 seed imports use validation and preserve durable operator decisions."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from db_helpers import local_main

from job_scout.discovery import http as public_http
from job_scout.discovery.detect import detect
from job_scout.discovery.registry import Registry
from job_scout.discovery.seeds import import_seeds
from job_scout.discovery.validate import Validation
from job_scout.models import SourceConfig, SourceRegistry
from job_scout.storage.database import initialize_database, open_database
from job_scout.storage.repository import Repository


@pytest.fixture
def registry(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/registry.db"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = open_database(url)
    initialize_database(engine)
    yield Registry(engine)
    engine.dispose()


def seed(board="example", **kwargs):
    return SourceConfig(source="lever", company="Example", board_id=board, enabled=True, **kwargs)


def run(registry, configs, handler, **kwargs):
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await import_seeds(SourceRegistry(sources=configs), registry, client, **kwargs)

    return asyncio.run(execute())


def valid_response(request):
    payload = json.loads(Path("tests/fixtures/discovery.json").read_text())
    return httpx.Response(200, json=payload["lever"])


def test_validated_seed_survives_restart_and_repeat_preserves_metadata(registry):
    result = run(registry, [seed()], valid_response)
    assert result["counts"] == {"active": 1}
    before = registry.sources()
    assert before[0]["validated_at"] is not None
    assert before[0]["origin"] == "manual_seed"
    assert Repository(registry.engine).jobs() == []
    url = str(registry.engine.url)
    registry.engine.dispose()
    restarted = open_database(url)
    try:
        restored = Registry(restarted)
        assert len(restored.active().sources) == 1
        result = run(restored, [seed()], lambda r: pytest.fail("No repeated validation"))
        assert result["counts"] == {"preserved": 1}
        assert restored.sources() == before
        restored.disable(before[0]["id"])
        run(restored, [seed()], lambda r: pytest.fail("Preserve explicit disable"))
        assert restored.active().sources == []
    finally:
        restarted.dispose()


@pytest.mark.parametrize("dry_run", [False, True])
def test_failed_and_ambiguous_seeds_never_activate(registry, dry_run):
    result = run(registry, [seed()], lambda r: httpx.Response(200, json=[]), dry_run=dry_run)
    assert result["counts"] == {"needs_review": 1}
    assert registry.active().sources == []
    assert bool(registry.candidates()) is not dry_run


def test_dry_run_validates_without_writes(registry):
    assert run(registry, [seed()], valid_response, dry_run=True)["counts"] == {"verified": 1}
    assert registry.sources() == []
    assert registry.candidates() == []


def test_shared_budget_defers_later_sources(registry):
    result = run(registry, [seed(), seed("other")], valid_response, max_requests=1)
    assert result["requests"] == 1
    assert result["counts"] == {"active": 1, "deferred": 1}
    assert len(registry.active().sources) == 1


def test_disabled_and_rejected_sources_make_no_requests(registry):
    candidate = detect("https://jobs.lever.co/rejected")
    saved = registry.record(candidate, Validation("needs_review", "Review", True))
    registry.reject(saved["id"])
    disabled = seed().model_copy(update={"enabled": False})
    result = run(registry, [disabled, seed("rejected")], lambda r: pytest.fail("No HTTP"))
    assert result["counts"] == {"disabled": 1, "rejected": 1}
    assert registry.active().sources == []


def test_runtime_budget_defers_inflight_and_remaining_sources(registry):
    async def slow(request):
        await asyncio.sleep(1)
        return valid_response(request)

    result = run(registry, [seed(), seed("other")], slow, max_runtime_seconds=0.01)
    assert result["counts"] == {"deferred": 2}
    assert registry.sources() == []


def test_cli_optional_seed_file_and_validated_import(registry, tmp_path, monkeypatch, capsys):
    path = tmp_path / "companies.yaml"
    assert local_main(["companies", "import-seeds", "--config", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["counts"] == {}
    path.write_text(
        "sources:\n  - source: lever\n    company: Example\n"
        "    board_id: example\n    enabled: true\n"
    )
    monkeypatch.setattr(
        public_http, "PublicAPITransport", lambda **kw: httpx.MockTransport(valid_response)
    )
    assert local_main(["companies", "import-seeds", "--config", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["counts"] == {"active": 1}
    assert registry.sources()[0]["validated_at"] is not None
