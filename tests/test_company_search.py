"""Company discovery acceptance: independent evidence, safe I/O and durable registry."""

import asyncio
import json
from datetime import date

import httpx
import pytest
from db_helpers import local_main
from sqlalchemy import select
from sqlalchemy.orm import Session

from job_scout import main as cli
from job_scout.collection import collect
from job_scout.discovery.companies.careers import CareersHTTP, SiteTransport, safe_url
from job_scout.discovery.companies.search import company_queries, discover_companies
from job_scout.discovery.registry import Registry
from job_scout.discovery.settings import DiscoverySettings
from job_scout.matching.profile import Profile
from job_scout.models import SourceRegistry
from job_scout.sources.base import BoardHTTP, SourceError
from job_scout.storage.database import CompanyRow, JobRow, initialize_database, open_database

SITE = "https://example.org"
BOARD = "https://jobs.ashbyhq.com/Example"


@pytest.fixture
def registry(tmp_path):
    engine = open_database(f"sqlite:///{tmp_path}/companies.db")
    initialize_database(engine)
    yield Registry(engine)
    engine.dispose()


def settings(**companies):
    return DiscoverySettings.model_validate(
        {
            "companies": {"enabled": True, "engineering_domains": ["robotics"], **companies},
            "providers": {"brave": {"max_queries": 1, "attempts": 1}},
        }
    )


def handler(request):
    url = str(request.url)
    if request.url.host == "api.search.brave.com":
        return httpx.Response(
            200,
            json={
                "query": {"more_results_available": False},
                "web": {
                    "results": [
                        {
                            "url": SITE + "/search-only-private-lead?secret=SEARCH_SENTINEL",
                            "description": "SEARCH_SENTINEL",
                            "title": "SEARCH_SENTINEL",
                        }
                    ]
                },
            },
        )
    if request.url.path == "/robots.txt":
        return httpx.Response(
            200, text="User-agent: *\nAllow: /", headers={"content-type": "text/plain"}
        )
    if url == SITE + "/":
        body = (
            '<script type="application/ld+json">'
            + json.dumps(
                {
                    "@type": "Organization",
                    "name": "Example Robotics",
                    "url": SITE,
                }
            )
            + '</script><p>We build robotics systems.</p><a href="/careers">Careers</a>'
        )
    elif url == SITE + "/careers":
        body = f'<a href="{BOARD}">Open roles</a>'
    elif url == BOARD:
        body = f'<a href="{SITE}">Example Robotics website</a>'
    elif request.url.host == "api.ashbyhq.com":
        return httpx.Response(200, json={"jobs": []})
    else:
        raise AssertionError(url)
    return httpx.Response(200, text=body, headers={"content-type": "text/html"})


def run(registry, config=None, respond=handler):
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:

            def http():
                return BoardHTTP(client, attempts=1, interval=0)

            return await discover_companies(
                http(), http(), http(), registry, Profile(), config or settings()
            )

    return asyncio.run(execute())


def test_empty_board_registered_without_jobs_and_monitored_after_restart(registry):
    result = run(registry)
    assert result.registered == 1 and not result.error
    sources = registry.sources()
    assert sources[0]["origin"] == "company_search"
    with Session(registry.engine) as session:
        company = session.scalar(select(CompanyRow))
        assert company.official_website == SITE
        assert company.display_name == "Example Robotics"
        assert session.scalar(select(JobRow)) is None
    # Search-only path, query, title and snippet are never retained.
    assert "SEARCH_SENTINEL" not in str(registry.candidates())
    assert "search-only-private-lead" not in str(registry.candidates())
    restarted = open_database(str(registry.engine.url))
    try:
        active = Registry(restarted).active()
        assert active.sources[0].board_id == "Example"

        async def monitor():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await collect(active, client=client, interval=0)

        assert not asyncio.run(monitor()).failed
    finally:
        restarted.dispose()
    assert run(registry).registered == 0
    registry.disable(sources[0]["id"])
    assert run(registry).registered == 0
    assert not registry.active().sources


@pytest.mark.parametrize(
    "case",
    [
        "missing_identity",
        "ambiguous",
        "irrelevant",
        "unsupported",
        "no_backlink",
        "robots",
        "redirect",
        "bad_api",
    ],
)
def test_unverified_never_registered(registry, case):
    def respond(request):
        url = str(request.url)
        if case == "robots" and request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /")
        response = handler(request)
        text = response.text
        if url == SITE + "/":
            if case == "missing_identity":
                text = "robotics <a href='/careers'>Careers</a>"
            elif case == "ambiguous":
                text += (
                    '<script type="application/ld+json">'
                    + json.dumps({"@type": "Organization", "name": "Different name", "url": SITE})
                    + "</script>"
                )
            elif case == "irrelevant":
                text = text.replace("robotics", "unrelated")
            elif case == "redirect":
                return httpx.Response(302, headers={"location": "http://169.254.169.254/"})
        if case == "unsupported" and url == SITE + "/careers":
            text = '<a href="https://example.myworkdayjobs.com/jobs">Roles</a>'
        if case == "no_backlink" and url == BOARD:
            text = '<a href="https://unrelated.org">Website</a>'
        if case == "bad_api" and request.url.host == "api.ashbyhq.com":
            return httpx.Response(200, json={"invalid": []})
        return httpx.Response(response.status_code, text=text, headers=response.headers)

    result = run(registry, respond=respond)
    assert result.registered == 0
    assert not registry.active().sources


def test_two_boards_share_verified_company(registry):
    def respond(request):
        if str(request.url) == SITE + "/careers":
            return httpx.Response(
                200,
                text=f'<a href="{BOARD}">Jobs</a>'
                '<a href="https://jobs.ashbyhq.com/Other">Other region</a>',
                headers={"content-type": "text/html"},
            )
        if str(request.url) == "https://jobs.ashbyhq.com/Other":
            return httpx.Response(
                200, text=f'<a href="{SITE}">Home</a>', headers={"content-type": "text/html"}
            )
        return handler(request)

    assert run(registry, respond=respond).registered == 2
    assert len({row["company_id"] for row in registry.sources()}) == 1


def test_company_queries_rotate_weekly():
    profile = Profile()
    config = settings(engineering_domains=["robotics", "distributed systems", "graphics"])
    first = company_queries(profile, config.companies, 1, date(2026, 10, 12))
    assert first != company_queries(profile, config.companies, 1, date(2026, 10, 19))
    assert "companies" in first[0] and "site:" not in first[0]


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org",
        "https://user:pass@example.org",
        "https://127.0.0.1",
        "https://server.internal",
        "https://example.org:8080",
        "file:///etc/passwd",
    ],
)
def test_site_url_rejects_unsafe(url):
    with pytest.raises(ValueError):
        safe_url(url)


def test_site_transport_rejects_private_dns():
    async def execute():
        async def resolve(host):
            return ["169.254.169.254"]

        transport = SiteTransport(
            resolver=resolve,
            transport=httpx.MockTransport(
                lambda request: pytest.fail("Private target must never be fetched")
            ),
        )
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(SourceError, match="non-public"):
                await client.get(SITE)

    asyncio.run(execute())


def test_robots_missing_allowed_but_redirects_never_followed():
    async def execute():
        calls = []

        def respond(request):
            calls.append(str(request.url))
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(302, headers={"location": "https://other.org"})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), follow_redirects=True
        ) as client:
            with pytest.raises(SourceError, match="302"):
                await CareersHTTP(BoardHTTP(client, interval=0)).page(SITE)
        assert len(calls) == 2

    asyncio.run(execute())


def test_dry_run_needs_no_db_key_and_writes_nothing(tmp_path, monkeypatch, capsys):
    path = tmp_path / "discovery.yaml"
    path.write_text("companies: {engineering_domains: [robotics]}")
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    assert (
        local_main(
            [
                "discover-companies",
                "--config",
                str(path),
                "--profile",
                "tests/fixtures/profile.yaml",
                "--dry-run",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run"


def test_search_failure_preserves_existing_registry(registry):
    run(registry)
    result = run(registry, respond=lambda request: httpx.Response(429))
    assert result.error and len(registry.active().sources) == 1


def test_site_budget_stops_requests(registry):
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            http = BoardHTTP(client, attempts=1, interval=0, max_requests=1)
            return await discover_companies(
                BoardHTTP(client, interval=0),
                http,
                BoardHTTP(client, interval=0),
                registry,
                Profile(),
                settings(),
            )

    result = asyncio.run(execute())
    assert result.site_requests == 1 and result.failed == 1
    assert not registry.sources()


def test_ingest_missing_optional_seeds(registry, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATABASE_URL", str(registry.engine.url))

    async def empty(active):
        assert active == SourceRegistry(sources=[])
        from job_scout.models import CollectionReport

        return CollectionReport(sources=[])

    monkeypatch.setattr(cli, "_collect", empty)
    assert local_main(["ingest", "--config", str(tmp_path / "absent.yaml")]) == 0


def test_companies_alias(registry, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", str(registry.engine.url))
    assert local_main(["companies", "list"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_noindex_meta_and_malformed_links_fail_safely():
    from job_scout.discovery.companies.careers import Page

    page = Page(
        '<meta name><a href="https://[broken">ignored</a>'
        '<script><a href="https://jobs.ashbyhq.com/evil">evil</a></script>',
        SITE,
    )
    assert page.links == []

    async def execute():
        def respond(request):
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(
                200,
                text='<meta name="robots" content="nofollow">',
                headers={"content-type": "text/html"},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(SourceError, match="opts out"):
                await CareersHTTP(BoardHTTP(client, interval=0)).page(SITE)

    asyncio.run(execute())


def test_search_budget_and_weekly_cooldown(registry, monkeypatch):
    from job_scout.discovery.budgets import SearchBudget
    from job_scout.discovery.companies import cli as company_cli
    from job_scout.discovery.providers.brave import BraveHTTP
    from job_scout.storage.insights import Insights

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            budget = SearchBudget(registry.engine, 1, 1)
            budget.reserve()  # Job-first search has already spent this account's allowance.
            return await discover_companies(
                BraveHTTP(client, settings().providers.brave, budget.reserve),
                BoardHTTP(client, interval=0),
                BoardHTTP(client, interval=0),
                registry,
                Profile(),
                settings(),
            )

    result = asyncio.run(execute())
    assert result.requests == 0 and result.error
    assert not registry.sources()
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "synthetic-key")
    assert Insights(registry.engine).claim_refresh("company_search", 168)
    result = asyncio.run(company_cli.fetch(settings(), Profile(), registry.engine))
    assert result.status == "not_due" and result.requests == 0


def test_runtime_budget_returns_partial_report(registry):
    async def execute():
        async def slow(request):
            await asyncio.sleep(0.05)
            return handler(request)

        config = settings()
        config.providers.brave.max_runtime_seconds = 0.001
        async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
            http = BoardHTTP(client, interval=0)
            return await discover_companies(http, http, http, registry, Profile(), config)

    result = asyncio.run(execute())
    assert "runtime" in result.error
    assert not registry.sources()


def test_conflicting_company_identity_rolls_back(registry):
    from job_scout.discovery.detect import DiscoveryError, detect
    from job_scout.discovery.validate import Validation

    run(registry)
    with pytest.raises(DiscoveryError, match="Conflicting"):
        registry.record(
            detect("https://jobs.ashbyhq.com/Unrelated", name="Different Company"),
            Validation("verified", "Synthetic", True),
            method="company_search",
            official_website=SITE,
        )
    assert len(registry.sources()) == 1
    assert len(registry.candidates()) == 1
