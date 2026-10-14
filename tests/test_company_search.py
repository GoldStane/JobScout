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


@pytest.mark.parametrize(
    "path,status,stage,code",
    [
        ("/robots.txt", 301, "robots", "http_redirect"),
        ("/", 403, "homepage", "http_denied"),
        ("/careers", 503, "careers", "http_server_error"),
        ("/Example", 429, "ats_page", "http_rate_limited"),
    ],
)
def test_diagnostics_identify_stage_without_leaking_urls(
    registry, caplog, path, status, stage, code
):
    import logging

    caplog.set_level(logging.INFO, logger="job_scout.discovery.companies.search")

    def respond(request):
        if request.url.host != "api.search.brave.com" and request.url.path == path:
            return httpx.Response(
                status,
                text="PRIVATE_SENTINEL",
                headers={"location": "https://PRIVATE_SENTINEL.org"},
            )
        return handler(request)

    report = run(registry, respond=respond)
    assert report.status == "partial" and report.failed == 1
    diagnostic = next(item for item in report.diagnostics if item["code"] == code)
    assert diagnostic["stage"] == stage and diagnostic["http_status"] == status
    assert diagnostic["site"] == 1 and diagnostic["action"]
    assert code in caplog.text
    assert SITE not in caplog.text and BOARD not in caplog.text
    assert "SENTINEL" not in caplog.text + json.dumps(report.as_dict())


def test_identity_missing_is_actionable_and_not_claimed_as_saved_candidate(registry):
    def respond(request):
        if str(request.url) == SITE + "/":
            return httpx.Response(
                200, text="No identity metadata", headers={"content-type": "text/html"}
            )
        return handler(request)

    report = run(registry, respond=respond)
    assert report.status == "no_new_boards"
    assert report.diagnostics[0]["code"] == "identity_missing"
    assert not registry.candidates()


def test_last_report_is_read_only_and_persists_only_safe_counts(registry, monkeypatch, capsys):
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    from job_scout.models import utc_now
    from job_scout.storage.database import DiscoveryRunRow
    from job_scout.storage.insights import Insights

    def respond(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(302, headers={"location": "https://PRIVATE_SENTINEL.org"})
        return handler(request)

    report = run(registry, respond=respond)
    Insights(registry.engine).record_companies(report, utc_now())
    Insights(registry.engine).claim_refresh("company_search", 168)
    with Session(registry.engine) as session:
        row = session.scalar(select(DiscoveryRunRow))
        assert row.counters["diagnostic:robots:http_redirect"] == 1
        assert "example.org" not in str(row.counters) and "SENTINEL" not in str(row.counters)

    def read_only(conn, cursor, statement, parameters, context, executemany):
        assert statement.lstrip().split()[0].upper() not in {"INSERT", "UPDATE", "DELETE"}

    monkeypatch.setenv("DATABASE_URL", str(registry.engine.url))
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    event.listen(Engine, "before_cursor_execute", read_only)
    try:
        assert local_main(["discover-companies", "--last-report"]) == 0
    finally:
        event.remove(Engine, "before_cursor_execute", read_only)
    saved = json.loads(capsys.readouterr().out)
    assert saved["status"] == "partial" and saved["next_due"]
    assert saved["diagnostics"][0]["code"] == "http_redirect"
    assert saved["diagnostics"][0]["count"] == 1


def test_legacy_report_marks_missing_diagnostics(registry):
    from job_scout.models import utc_now
    from job_scout.storage.database import DiscoveryRunRow
    from job_scout.storage.insights import Insights

    with Session(registry.engine) as session, session.begin():
        session.add(
            DiscoveryRunRow(
                provider="company_search",
                attempted_at=utc_now(),
                completed_at=utc_now(),
                status="partial",
                counters={"failed": 4},
            )
        )
    saved = Insights(registry.engine).last_company_report()
    assert not saved["diagnostics_available"] and saved["counters"]["failed"] == 4


def test_retry_now_preserves_account_quotas(registry, monkeypatch):
    from job_scout.discovery.budgets import SearchBudget
    from job_scout.discovery.companies import cli as company_cli
    from job_scout.discovery.http import PublicAPITransport
    from job_scout.storage.insights import Insights

    config = settings()
    config.providers.brave.daily_request_limit = 1
    config.providers.brave.monthly_request_limit = 1
    SearchBudget(registry.engine, 1, 1).reserve()
    assert Insights(registry.engine).claim_refresh("company_search", 168)
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "PRIVATE_SENTINEL")

    async def forbidden(*args, **kwargs):
        pytest.fail("An explicit cooldown retry must not bypass the shared request allowance")

    monkeypatch.setattr(PublicAPITransport, "handle_async_request", forbidden)
    report = asyncio.run(company_cli.fetch(config, Profile(), registry.engine, retry_now=True))
    assert report.status == "failed" and report.requests == 0 and report.next_due
    assert report.diagnostics[0]["code"] == "request_budget"
    assert "PRIVATE_SENTINEL" not in str(report)


def test_retry_dry_run_does_not_claim_schedule_or_search(tmp_path, monkeypatch, capsys):
    path = tmp_path / "discovery.yaml"
    path.write_text("companies: {enabled: true, engineering_domains: [robotics]}")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    assert (
        cli.main(
            [
                "discover-companies",
                "--config",
                str(path),
                "--profile",
                "tests/fixtures/profile.yaml",
                "--retry-now",
                "--dry-run",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run"


def test_partial_company_report_cli_returns_nonzero(registry, monkeypatch, tmp_path, capsys):
    from job_scout.discovery.companies import cli as company_cli
    from job_scout.discovery.companies.search import CompanyReport

    async def fake_fetch(*args, **kwargs):
        report = CompanyReport(status="partial", failed=4, pending_review=1, limited=True)
        report.note("robots", "http_redirect", site=1, query=1, http_status=301)
        return report

    monkeypatch.setattr(company_cli, "fetch", fake_fetch)
    monkeypatch.setenv("DATABASE_URL", str(registry.engine.url))
    path = tmp_path / "discovery.yaml"
    path.write_text("companies: {enabled: true}")
    assert (
        local_main(
            [
                "discover-companies",
                "--config",
                str(path),
                "--profile",
                "tests/fixtures/profile.yaml",
            ]
        )
        == 1
    )
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "partial" and output["diagnostics"][0]["code"] == "http_redirect"


def test_unknown_error_and_diagnostic_volume_are_bounded():
    from job_scout.discovery.companies.diagnostics import classify
    from job_scout.discovery.companies.search import CompanyReport

    diagnostic = classify(SourceError("PRIVATE_SENTINEL https://example.org?token=secret"))
    assert diagnostic == {"code": "verification_error"}
    report = CompanyReport()
    for _ in range(205):
        report.note("registration", "already_known")
    assert len(report.diagnostics) == 200 and report.diagnostics_omitted == 5


def test_five_site_failure_report_explains_every_outcome(registry):
    def respond(request):
        if request.url.host == "api.search.brave.com":
            return httpx.Response(
                200,
                json={
                    "query": {"more_results_available": False},
                    "web": {"results": [{"url": f"https://candidate{i}.org/"} for i in range(6)]},
                },
            )
        if request.url.host in {f"candidate{i}.org" for i in range(4)}:
            return httpx.Response(301, headers={"location": "https://PRIVATE_SENTINEL.org/"})
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, text="A company page without metadata", headers={"content-type": "text/html"}
        )

    report = run(registry, respond=respond)
    assert report.status == "partial" and report.error is None
    assert report.sites == 5 and report.failed == 4 and report.pending_review == 1
    assert report.registered == 0 and report.publisher_requests == 0 and report.limited
    codes = [item["code"] for item in report.diagnostics]
    assert codes.count("http_redirect") == 4
    assert "identity_missing" in codes and "site_limit" in codes
    assert ".org" not in json.dumps(report.diagnostics)


@pytest.mark.parametrize("case", ["match", "different", "missing", "ambiguous", "no_backlink"])
def test_branding_fallback_requires_independent_ats_confirmation(registry, case):
    def respond(request):
        if str(request.url) == SITE + "/":
            body = (
                '<meta property="og:site_name" content="Example Robotics">'
                '<p>robotics</p><a href="/careers">Careers</a>'
            )
            if case == "ambiguous":
                body += '<meta property="og:site_name" content="Other Company">'
            return httpx.Response(200, text=body, headers={"content-type": "text/html"})
        if str(request.url) == BOARD:
            name = "Different Company" if case == "different" else "Example Robotics"
            body = "" if case == "missing" else f'<meta property="og:site_name" content="{name}">'
            if case != "no_backlink":
                body += f'<a href="{SITE}">Website</a>'
            return httpx.Response(200, text=body, headers={"content-type": "text/html"})
        return handler(request)

    report = run(registry, respond=respond)
    assert report.registered == (1 if case == "match" else 0)
    assert bool(registry.active().sources) == (case == "match")


def test_corporation_metadata_can_identify_employer(registry):
    def respond(request):
        response = handler(request)
        if str(request.url) == SITE + "/":
            return httpx.Response(
                200,
                text=response.text.replace('"Organization"', '"Corporation"'),
                headers=response.headers,
            )
        return response

    assert run(registry, respond=respond).registered == 1


def test_site_budget_distributed_across_queries_despite_denials(registry):
    queries = []
    visits = []
    config = settings(engineering_domains=["robotics", "graphics", "distributed systems"])
    config.providers.brave.max_queries = 3

    def respond(request):
        if request.url.host == "api.search.brave.com":
            queries.append(request.url.params["q"])
            prefix = len(queries)
            return httpx.Response(
                200,
                json={
                    "query": {"more_results_available": True},
                    "web": {
                        "results": [
                            {"url": f"https://candidate{prefix}-{i}.org/"} for i in range(10)
                        ]
                    },
                },
            )
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        visits.append(request.url.host)
        return httpx.Response(403)

    report = run(registry, config=config, respond=respond)
    assert len(queries) == report.queries == 3
    assert report.sites == report.failed == 5
    assert [sum(host.startswith(f"candidate{i}-") for host in visits) for i in (1, 2, 3)] == [
        2,
        2,
        1,
    ]
    assert all("credentials" not in item["action"] for item in report.diagnostics)


def test_search_denial_has_search_specific_action(registry):
    report = run(registry, respond=lambda request: httpx.Response(403))
    assert report.diagnostics[0]["code"] == "search_denied"
    assert "credentials" in report.diagnostics[0]["action"]


def test_queries_prefer_roles_to_individual_skills():
    profile = Profile.model_validate(
        {
            "skills": ["Python"],
            "preferences": {"primary_roles": ["systems engineer"], "secondary_roles": []},
        }
    )
    config = settings(engineering_domains=[])
    queries = company_queries(profile, config.companies, 3, date(2026, 10, 12))
    assert len(queries) == 1 and "systems engineer" in queries[0]
    assert "Python" not in queries[0]
    config.companies.engineering_domains = ["embedded systems"]
    assert (
        "embedded systems" in company_queries(profile, config.companies, 3, date(2026, 10, 12))[0]
    )


def test_careers_canonical_redirect_registers_board(registry):
    def respond(request):
        if str(request.url) == SITE + "/careers":
            return httpx.Response(301, headers={"location": "/careers/"})
        if str(request.url) == SITE + "/careers/":
            return httpx.Response(
                200, text=f'<a href="{BOARD}">Jobs</a>', headers={"content-type": "text/html"}
            )
        return handler(request)

    report = run(registry, respond=respond)
    assert report.registered == 1
    assert report.failed == 0


@pytest.mark.parametrize(
    "destination",
    [
        "https://other.org/jobs",
        "http://example.org/jobs",
        "https://127.0.0.1/jobs",
        "https://user:secret@example.org/jobs",
        "/careers",
        "https://[broken",
    ],
)
def test_redirect_rejected_without_fetching_target(destination):
    async def execute():
        calls = []

        def respond(request):
            calls.append(str(request.url))
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            assert str(request.url) == SITE + "/careers"
            return httpx.Response(301, headers={"location": destination})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(SourceError, match="HTTP 301"):
                await CareersHTTP(BoardHTTP(client, interval=0)).page(SITE + "/careers")
        assert len(calls) == 2

    asyncio.run(execute())


def test_redirect_rechecks_robots_for_destination():
    async def execute():
        def respond(request):
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nDisallow: /private")
            assert request.url.path == "/careers"
            return httpx.Response(302, headers={"location": "/private"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(SourceError, match="Publisher page fetch failed"):
                await CareersHTTP(BoardHTTP(client, interval=0)).page(SITE + "/careers")

    asyncio.run(execute())


@pytest.mark.parametrize("max_redirects,max_requests,expected", [(2, 20, 4), (5, 3, 3), (0, 20, 2)])
def test_redirect_hops_and_requests_remain_bounded(max_redirects, max_requests, expected):
    async def execute():
        def respond(request):
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(301, headers={"location": request.url.path + "/next"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            http = BoardHTTP(client, interval=0, max_requests=max_requests)
            with pytest.raises(SourceError):
                await CareersHTTP(http, max_redirects=max_redirects).page(SITE + "/careers")
            assert http.requests == expected

    asyncio.run(execute())


def test_unused_query_shares_return_to_deferred_candidates(registry):
    searches = []
    config = settings(engineering_domains=["robotics", "graphics", "distributed systems"])
    config.providers.brave.max_queries = 3

    def respond(request):
        if request.url.host == "api.search.brave.com":
            searches.append(request.url.params["q"])
            urls = [f"https://candidate{i}.org/" for i in range(4)] if len(searches) == 1 else []
            return httpx.Response(
                200,
                json={
                    "query": {"more_results_available": False},
                    "web": {"results": [{"url": url} for url in urls]},
                },
            )
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(403)

    report = run(registry, config=config, respond=respond)
    assert report.queries == report.requests == 3
    assert report.sites == report.failed == 4
    assert not report.limited
    assert {item["query"] for item in report.diagnostics} == {1}


def test_search_exhaustion_still_verifies_deferred_lead(registry):
    calls = 0
    config = settings(engineering_domains=["robotics", "graphics", "systems"])
    config.providers.brave.max_queries = 3

    def respond(request):
        nonlocal calls
        if request.url.host == "api.search.brave.com":
            calls += 1
            if calls > 1:
                return httpx.Response(403)
            return httpx.Response(
                200,
                json={
                    "query": {"more_results_available": False},
                    "web": {
                        "results": [
                            {"url": "https://denied1.org"},
                            {"url": "https://denied2.org"},
                            {"url": SITE},
                        ]
                    },
                },
            )
        if request.url.host.startswith("denied"):
            return httpx.Response(403)
        return handler(request)

    result = run(registry, config=config, respond=respond)
    assert result.registered == 1 and result.error
    assert result.status == "partial"
    assert result.queries == 2 and calls == 2


@pytest.mark.parametrize("daily,monthly,scope", [(1, 10, "daily"), (10, 1, "monthly")])
def test_budget_failure_reports_exact_window_and_reset(registry, daily, monthly, scope):
    from datetime import UTC, datetime
    from job_scout.discovery.budgets import BudgetExceeded, SearchBudget

    budget = SearchBudget(registry.engine, daily, monthly)
    now = datetime(2026, 12, 31, 20, tzinfo=UTC)
    budget.reserve(now)
    with pytest.raises(BudgetExceeded) as caught:
        budget.reserve(now)
    assert caught.value.quota == {
        "scope": scope,
        "used": 1,
        "limit": 1,
        "reset_at": "2027-01-01T00:00:00+00:00",
    }
    budget.reserve(datetime(2027, 1, 1, tzinfo=UTC))


def test_safe_publisher_paths_do_not_require_board_identifier():
    assert safe_url("https://jobs.ashbyhq.com/robots.txt").endswith("/robots.txt")
    assert safe_url("https://jobs.ashbyhq.com/") == "https://jobs.ashbyhq.com/"


def test_specific_diagnostics_never_echo_exception_content():
    from job_scout.discovery.companies.diagnostics import classify
    from job_scout.discovery.detect import DiscoveryError

    assert classify(DiscoveryError("Company pages require HTTPS"))["code"] == "https_required"
    assert (
        classify(DiscoveryError("URL does not contain a valid board identifier"))["code"]
        == "invalid_board_identifier"
    )


def test_saved_quota_is_sanitized_and_available(registry):
    from job_scout.discovery.companies.search import CompanyReport
    from job_scout.models import utc_now
    from job_scout.storage.insights import Insights

    report = CompanyReport(
        quota={
            "scope": "daily",
            "used": 6,
            "limit": 6,
            "reset_at": "2026-10-14T00:00:00+00:00",
            "token": "SECRET",
        }
    )
    Insights(registry.engine).record_companies(report, utc_now())
    saved = Insights(registry.engine).last_company_report()
    assert saved["quota"]["scope"] == "daily"
    assert "SECRET" not in str(saved)
