"""Offline discovery, approval and canonical-registry acceptance tests."""

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from db_helpers import local_main
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from job_scout import main as cli
from job_scout.discovery import cli as discovery_cli
from job_scout.discovery.detect import DiscoveryError, detect
from job_scout.discovery.registry import Registry
from job_scout.discovery.validate import Validation, validate
from job_scout.models import SourceRegistry, utc_now
from job_scout.sources.base import BoardHTTP
from job_scout.storage.database import (
    CandidateRow,
    CompanyRow,
    DigestRow,
    JobRow,
    NotificationRow,
    SchemaVersion,
    SourceRow,
    initialize_database,
    migrate_database,
    open_database,
)
from job_scout.storage.delivery import DeliveryRepository, recipient_key

FIXTURES = json.loads(Path("tests/fixtures/discovery.json").read_text())
URLS = {
    "greenhouse": "https://boards.greenhouse.io/example",
    "lever": "https://jobs.lever.co/example",
    "ashby": "https://jobs.ashbyhq.com/example",
}


@pytest.fixture
def registry(tmp_path):
    engine = open_database(f"sqlite:///{tmp_path}/discovery.db")
    initialize_database(engine)
    yield Registry(engine)
    engine.dispose()


def verify(url, response, **kwargs):
    requests = []

    async def execute():
        def handler(request):
            requests.append(request)
            if isinstance(response, Exception):
                raise response
            return response

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ) as client:
            return await validate(detect(url), client, interval=0, **kwargs)

    return asyncio.run(execute()), requests


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@jobs.lever.co/example",
        "http://127.0.0.1/a",
        "http://[::1]/a",
        "http://169.254.169.254/a",
        "http://10.0.0.1/a",
        "http://localhost/a",
        "file:///etc/passwd",
        "https://server.internal/a",
        "https://jobs.lever.co:8080/example",
        "https://jobs.lever.co/%2e%2e",
        "https://jobs.lever.co/%2fexample",
        "https://jobs.lever.co/",
        "https://jobs.lever.co\\@evil.example/a",
        "https://jobs.lever.co/REPLACE_WITH_BOARD",
    ],
)
def test_unsafe_or_invalid_input_rejected(url):
    with pytest.raises(DiscoveryError):
        detect(url)


def test_normalization_and_eu_identity():
    a = detect("http://BOARDS.GREENHOUSE.IO/example/jobs/12?utm_source=x#fragment")
    b = detect("https://job-boards.greenhouse.io/example/")
    assert a == b
    assert detect("https://jobs.eu.lever.co/example/1").config.region == "eu"
    assert detect("https://jobs.eu.lever.co/example").identity != detect(URLS["lever"]).identity


@pytest.mark.parametrize(
    "url",
    [
        "https://careers.example.org/jobs",
        "https://jobs.lever.co.evil.example/example",
        "https://careers.smartrecruiters.com/example",
    ],
)
def test_unsupported_is_reviewable_without_http(url):
    result, requests = verify(url, httpx.Response(200, json=[]))
    assert result.state == "needs_review" and not result.endpoint_valid
    assert requests == []


@pytest.mark.parametrize("provider", list(URLS))
def test_known_board_validated_using_existing_adapters(provider):
    result, requests = verify(URLS[provider], httpx.Response(200, json=FIXTURES[provider]))
    assert result.state == "verified" and result.jobs_found == 1
    assert requests[0].url.host in {"boards-api.greenhouse.io", "api.lever.co", "api.ashbyhq.com"}


@pytest.mark.parametrize(
    "target",
    ["http://127.0.0.1/private", "https://jobs.lever.co/example", "https://evil.example/a"],
)
def test_all_redirects_rejected_even_when_client_follows_them(target):
    result, requests = verify(URLS["lever"], httpx.Response(302, headers={"Location": target}))
    assert result.state == "pending_validation" and len(requests) == 1


@pytest.mark.parametrize("payload", [{}, {"jobs": "wrong"}, {"jobs": [{}]}])
def test_malformed_api_response_retryable(payload):
    result, _ = verify(URLS["greenhouse"], httpx.Response(200, json=payload))
    assert result.state == "pending_validation" and not result.endpoint_valid


def test_empty_or_identity_mismatch_requires_review():
    result, _ = verify(URLS["lever"], httpx.Response(200, json=[]))
    assert result.state == "needs_review" and result.endpoint_valid
    result, _ = verify("https://jobs.lever.co/wrong", httpx.Response(200, json=FIXTURES["lever"]))
    assert result.state == "needs_review" and "identity" in result.reason


def test_bounds_and_rate_limits(monkeypatch):
    async def no_sleep(_):
        pass

    monkeypatch.setattr(BoardHTTP, "_pace", lambda self: no_sleep(0))
    result, requests = verify(URLS["lever"], httpx.Response(200, text="x" * 2048), max_bytes=1024)
    assert result.state == "pending_validation" and "size" in result.reason
    result, requests = verify(URLS["lever"], httpx.Response(429, headers={"Retry-After": "3600"}))
    assert len(requests) == 1 and result.state == "pending_validation"
    result, requests = verify(URLS["lever"], httpx.ReadTimeout("private details"), max_requests=1)
    assert len(requests) == 1 and result.state == "pending_validation"
    assert "private details" not in result.reason


def test_idempotence_seeds_and_disabled_override(registry):
    candidate = detect(URLS["lever"])
    good = Validation("verified", "Valid board", True, 1)
    first = registry.record(candidate, good)
    assert registry.record(candidate, good)["id"] == first["id"]
    registry.disable(first["source_id"])
    registry.seed(SourceRegistry(sources=[candidate.config.model_copy(update={"enabled": True})]))
    registry.record(candidate, good)
    assert not registry.active().sources
    registry.enable(first["source_id"])
    assert len(registry.active().sources) == 1
    with Session(registry.engine) as session:
        assert session.scalar(select(func.count()).select_from(CompanyRow)) == 1
        assert session.scalar(select(func.count()).select_from(SourceRow)) == 1
        assert session.scalar(select(func.count()).select_from(CandidateRow)) == 1


def test_approval_failure_isolation_rejection_and_stale_validation(registry):
    candidate = detect(URLS["lever"])
    row = registry.record(candidate, Validation("needs_review", "Empty board", True))
    assert not registry.active().sources
    registry.approve(row["id"])
    assert registry.active().sources
    registry.record(candidate, Validation("pending_validation", "HTTP 503"))
    assert registry.active().sources  # A discovery failure cannot disable monitoring.
    with pytest.raises(DiscoveryError):
        registry.approve(row["id"])
    registry.record(candidate, Validation("needs_review", "Empty board", True))
    with Session(registry.engine) as session, session.begin():
        session.get(CandidateRow, row["id"]).validated_at = utc_now() - timedelta(days=2)
    with pytest.raises(DiscoveryError, match="24 hours"):
        registry.approve(row["id"])
    registry.reject(row["id"])
    assert registry.record(candidate, Validation("verified", "OK", True))["state"] == "rejected"
    assert not registry.active().sources


def test_distinct_boards_not_merged_by_name_and_explicit_company_link(registry):
    first = registry.record(
        detect(URLS["lever"], name="Same name"), Validation("verified", "OK", True)
    )
    company_id = registry.sources()[0]["company_id"]
    registry.record(
        detect(URLS["ashby"], name="Same name"),
        Validation("verified", "OK", True),
        company_id=company_id,
    )
    registry.record(
        detect(URLS["greenhouse"], name="Same name"), Validation("verified", "OK", True)
    )
    assert len(registry.sources()) == 3
    assert first["source_id"] == registry.sources()[0]["id"]
    with Session(registry.engine) as session:
        assert session.scalar(select(func.count()).select_from(CompanyRow)) == 2


def test_dry_run_does_not_open_database(monkeypatch, capsys):
    async def fake(candidate, client, **kwargs):
        return Validation("verified", "OK", True)

    monkeypatch.setattr(discovery_cli, "validate", fake)
    monkeypatch.setattr(
        discovery_cli, "open_database", lambda: pytest.fail("No database for preview")
    )
    assert local_main(["company", "add", URLS["lever"], "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"]


def test_registered_source_is_collected_without_yaml(registry, monkeypatch, capsys):
    monkeypatch.setattr(cli, "open_database", lambda: registry.engine)
    registry.record(detect(URLS["lever"]), Validation("verified", "OK", True))
    from job_scout.models import CollectionReport, SourceResult

    async def fake_collect(sources):
        assert sources.sources[0].board_id == "example"
        return CollectionReport(
            sources=[
                SourceResult(
                    config=sources.sources[0], attempted_at=utc_now(), completed_at=utc_now()
                )
            ]
        )

    monkeypatch.setattr(cli, "_collect", fake_collect)
    assert local_main(["ingest", "--registry-only"]) == 0
    assert registry.sources()[0]["last_success"] is not None


def test_real_revision_two_migration_keeps_delivery_history(tmp_path):
    engine = open_database(f"sqlite:///{tmp_path}/legacy.db")
    # Actual old source columns, not a version-number-only simulation.
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE sources (id INTEGER PRIMARY KEY, provider VARCHAR(32) NOT NULL, "
                "region VARCHAR(16) NOT NULL, board_id VARCHAR(200) NOT NULL, "
                "company TEXT NOT NULL, "
                "last_attempt DATETIME, last_success DATETIME, last_error TEXT, "
                "consecutive_failures INTEGER NOT NULL, UNIQUE(provider, region, board_id))"
            )
        )
        for table in [
            SchemaVersion.__table__,
            JobRow.__table__,
            DigestRow.__table__,
            NotificationRow.__table__,
        ]:
            table.create(connection)
        from job_scout.storage.database import DeliveryLock

        DeliveryLock.__table__.create(connection)
        connection.execute(SchemaVersion.__table__.insert().values(id=1, version=2))
        connection.execute(
            text(
                "INSERT INTO sources (id,provider,region,board_id,company,consecutive_failures) "
                "VALUES (1,'lever','global','example','Example',0)"
            )
        )
        connection.execute(
            DigestRow.__table__.insert().values(
                id=1,
                recipient=recipient_key("123"),
                local_date="2026-10-09",
                timezone="Europe/Rome",
                created_at=utc_now(),
                completed_at=utc_now(),
            )
        )
        connection.execute(
            NotificationRow.__table__.insert().values(
                digest_id=1, part_index=0, text="Sent", job_keys=["old"], status="sent", attempts=1
            )
        )
    migrate_database(engine)
    migrate_database(engine)
    assert DeliveryRepository(engine).delivered_keys(recipient_key("123")) == {"old"}
    registry = Registry(engine)
    assert registry.sources()[0]["status"] == "unregistered"
    registry.seed(
        SourceRegistry(sources=[detect(URLS["lever"]).config.model_copy(update={"enabled": True})])
    )
    assert registry.sources()[0]["id"] == 1
    assert registry.sources()[0]["status"] == "active"
    engine.dispose()


@pytest.mark.parametrize(
    "addresses",
    [["127.0.0.1"], ["169.254.169.254"], ["::1"], ["10.0.0.1"], ["8.8.8.8", "192.168.1.1"], []],
)
def test_api_dns_cannot_resolve_private_addresses(addresses):
    from job_scout.discovery.http import PublicAPITransport
    from job_scout.sources.base import SourceError

    async def resolver(host):
        return addresses

    async def execute():
        transport = PublicAPITransport(
            resolver=resolver,
            transport=httpx.MockTransport(lambda request: pytest.fail("Must not connect")),
        )
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(SourceError):
                await client.get("https://api.lever.co/v0/postings/example")

    asyncio.run(execute())


def test_public_dns_pinned_with_original_tls_hostname():
    from job_scout.discovery.http import PublicAPITransport

    resolutions = []

    async def resolver(host):
        resolutions.append(host)
        return ["8.8.8.8"]  # Synthetic public address; no live network.

    def handler(request):
        assert request.url.host == "8.8.8.8"
        assert request.headers["Host"] == "api.lever.co"
        assert request.extensions["sni_hostname"] == "api.lever.co"
        return httpx.Response(200, json=[])

    async def execute():
        async with httpx.AsyncClient(
            transport=PublicAPITransport(resolver=resolver, transport=httpx.MockTransport(handler))
        ) as client:
            assert (await client.get("https://api.lever.co/v0/postings/example")).json() == []

    asyncio.run(execute())
    assert resolutions == ["api.lever.co"]


def test_persisted_disabled_source_wins_in_preview(registry):
    candidate = detect(URLS["lever"])
    seed = candidate.config.model_copy(update={"enabled": False})
    registry.seed(SourceRegistry(sources=[seed]))
    enabled = SourceRegistry(sources=[seed.model_copy(update={"enabled": True})])
    assert not registry.active(enabled).sources
    assert registry.sources()[0]["origin"] == "manual_seed"


def test_cli_add_then_revalidate_is_idempotent(registry, monkeypatch, capsys):
    monkeypatch.setattr(discovery_cli, "open_database", lambda: registry.engine)
    results = iter(
        [Validation("pending_validation", "HTTP 429"), Validation("verified", "OK", True)]
    )

    async def fake(candidate, client, **kwargs):
        return next(results)

    monkeypatch.setattr(discovery_cli, "validate", fake)
    assert local_main(["company", "add", URLS["lever"]]) == 1
    candidate = json.loads(capsys.readouterr().out)
    assert candidate["state"] == "pending_validation"
    assert local_main(["company", "revalidate", str(candidate["id"])]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "active"
    assert len(registry.candidates()) == 1
    assert len(registry.active().sources) == 1


def test_unsupported_manual_seed_cannot_abort_good_monitoring():
    from job_scout.collection import collect
    from job_scout.models import SourceConfig

    async def execute():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
        ) as client:
            return await collect(
                SourceRegistry(
                    sources=[
                        SourceConfig(
                            source="smartrecruiters",
                            board_id="example",
                            company="Unsupported",
                            enabled=True,
                        ),
                        detect(URLS["lever"]).config.model_copy(update={"enabled": True}),
                    ]
                ),
                client=client,
                interval=0,
            )

    result = asyncio.run(execute())
    assert result.sources[0].error
    assert result.sources[1].error is None
