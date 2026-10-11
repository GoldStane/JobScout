"""M4 contracts, profile queries and both acquisition paths, entirely offline."""

import asyncio
import copy
import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from job_scout.discovery.orchestrator import discover
from job_scout.discovery.providers.himalayas import HimalayasProvider
from job_scout.discovery.queries import profile_queries
from job_scout.discovery.settings import DiscoverySettings, HimalayasSettings
from job_scout.matching.profile import Profile
from job_scout.sources.base import BoardHTTP

FIXTURE = Path(__file__).parent / "fixtures/himalayas.json"


@pytest.fixture
def payload():
    return json.loads(FIXTURE.read_text())


def collect(handler, *, queries=None, **options):
    delays = []

    async def sleep(seconds):
        delays.append(seconds)

    async def run():
        settings = HimalayasSettings(**options)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            http = BoardHTTP(
                client,
                interval=0,
                sleep=sleep,
                attempts=settings.attempts,
                max_requests=settings.max_requests,
                max_bytes=settings.max_response_bytes,
                minimum_rate_limit_delay=60,
            )
            result = await discover(HimalayasProvider(http, settings), settings, queries=queries)
            result.requests = http.requests
            return result

    return asyncio.run(run()), delays


def test_fixture_normalizes_restrictions_salary_dates_and_attribution(payload):
    result, _ = collect(lambda request: httpx.Response(200, json=payload))
    assert result.error is None and result.pages == result.requests == 1
    first, second = result.jobs
    assert first.source == "himalayas" and first.company_identifier == "feed"
    assert len(first.source_job_id) == 64
    assert first.source_metadata["guid"] == payload["jobs"][0]["guid"]
    assert first.description_text == "Create accessible interfaces and prototypes."
    assert first.location_text == "Canada, United States"
    assert first.remote_type == "remote"
    assert first.timezone_restrictions == ["-8", "-5"]
    assert first.salary_text == "50000–70000 CAD / year"
    assert first.posted_at.timestamp() == 1791492012
    assert first.application_url is None and first.listing_url == first.source_url
    assert first.attribution == "Himalayas"
    assert str(first.attribution_url) == "https://himalayas.app/"
    assert first.eligibility_information == {}
    assert second.location_text == "Worldwide (remote)"
    assert second.salary_text is None
    assert second.description_text == "Edit clear documentation."


def test_documented_milliseconds_country_objects_and_unknowns(payload):
    item = payload["jobs"][0]
    item.update(pubDate=1791492012000, locationRestrictions=[{"name": "Canada", "code": "CA"}])
    del item["salaryPeriod"]
    del item["timezoneRestrictions"]
    del payload["jobs"][1]["locationRestrictions"]
    result, _ = collect(lambda request: httpx.Response(200, json=payload))
    first, second = result.jobs
    assert first.posted_at.timestamp() == 1791492012
    assert first.location_text == "Canada"
    assert first.timezone_restrictions is None
    assert first.salary_text.endswith("period unknown")
    assert second.location_text == "Remote (restrictions unknown)"


@pytest.mark.parametrize(
    "change",
    [
        {"guid": ""},
        {"applicationLink": "http://127.0.0.1/jobs/a"},
        {"applicationLink": "https://user:password@himalayas.app/companies/a/jobs/b"},
        {"applicationLink": "https://himalayas.app.attacker.test/companies/a/jobs/b"},
        {"applicationLink": "https://himalayas.app:444/companies/a/jobs/b"},
        {"pubDate": "2026-10-01"},
        {"pubDate": True},
        {"minSalary": -1},
        {"minSalary": 90000},
        {"locationRestrictions": "Canada"},
    ],
)
def test_invalid_posting_does_not_discard_healthy_job(payload, change):
    payload["jobs"][0].update(change)
    result, _ = collect(lambda request: httpx.Response(200, json=payload))
    assert result.skipped == 1 and len(result.jobs) == 1 and result.error is None


def test_expired_pages_continue_using_opaque_cursor(payload):
    first = copy.deepcopy(payload)
    first["jobs"] = [first["jobs"][0]]
    first["jobs"][0]["expiryDate"] = 1
    first["nextCursor"] = "opaque+/=?value"
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=first if len(seen) == 1 else payload)

    result, _ = collect(handler, page_size=2)
    assert seen == [{"limit": "2"}, {"limit": "2", "cursor": "opaque+/=?value"}]
    assert result.expired == 1 and len(result.jobs) == 2 and result.pages == 2


@pytest.mark.parametrize("envelope", [[], {}, {"jobs": None}, {"jobs": [], "nextCursor": 1}])
def test_invalid_envelope_is_provider_failure(envelope):
    result, _ = collect(lambda request: httpx.Response(200, json=envelope))
    assert result.error and not result.jobs


def test_empty_feed_ends_pagination():
    result, _ = collect(lambda request: httpx.Response(200, json={"jobs": []}))
    assert result.pages == 1 and not result.jobs and not result.error


def test_page_and_request_limits_preserve_observations(payload):
    payload["nextCursor"] = "opaque"
    result, _ = collect(lambda request: httpx.Response(200, json=payload), max_pages=1)
    assert result.limited and len(result.jobs) == 2 and result.requests == 1
    result, _ = collect(lambda request: httpx.Response(200, json=payload), max_requests=1)
    assert "Request budget" in result.error and len(result.jobs) == 2
    result, _ = collect(lambda request: httpx.Response(200, json=payload))
    assert "Repeated feed page" in result.error and result.requests == 2


def test_search_filters_pagination_query_rotation_and_duplicate_yield(payload):
    requests = []

    def handler(request):
        assert request.url.path == "/jobs/api/search"
        requests.append(dict(request.url.params))
        return httpx.Response(200, json=payload | {"totalCount": 21})

    result, _ = collect(handler, mode="search", country="Canada", queries=["designer", "editor"])
    assert requests == [
        {"page": page, "sort": "recent", "q": query, "country": "Canada"}
        for query in ["designer", "editor"]
        for page in ["1", "2"]
    ]
    assert result.pages == 4 and result.queries == 2 and len(result.jobs) == 2
    assert result.error is None


def test_search_worldwide_and_unfiltered_profile(payload):
    def handler(request):
        assert dict(request.url.params) == {"page": "1", "sort": "recent", "worldwide": "true"}
        return httpx.Response(200, json=payload)

    result, _ = collect(handler, mode="search", worldwide_only=True)
    assert len(result.jobs) == 2


@pytest.mark.parametrize("extra", [{"limit": 0}, {"totalCount": "2"}, {"limit": True}])
def test_invalid_search_pagination(payload, extra):
    result, _ = collect(lambda request: httpx.Response(200, json=payload | extra), mode="search")
    assert result.error and not result.jobs


def test_rate_limit_retry_timeout_redirect_and_size_bounds(payload):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429) if len(calls) == 1 else httpx.Response(200, json=payload)

    result, delays = collect(handler)
    assert result.requests == 2 and result.error is None
    assert 60 in delays
    result, _ = collect(lambda r: httpx.Response(429, headers={"Retry-After": "120"}))
    assert result.requests == 1 and "Retry-After" in result.error

    def timeout(request):
        raise httpx.ReadTimeout("timeout", request=request)

    result, _ = collect(timeout, attempts=2)
    assert result.requests == 2 and "ReadTimeout" in result.error
    result, _ = collect(lambda r: httpx.Response(302, headers={"Location": "http://127.0.0.1"}))
    assert result.requests == 1 and "302" in result.error
    result, _ = collect(lambda r: httpx.Response(200, content=b"x" * 2000), max_response_bytes=1024)
    assert "size limit" in result.error


def test_runtime_budget():
    async def slow(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, json={"jobs": []})

    result, _ = collect(slow, max_runtime_seconds=0.01)
    assert "runtime budget" in result.error


def test_profile_queries_are_compact_deterministic_and_rotate():
    profile = Profile.model_validate(
        {
            "preferences": {
                "primary_roles": [" Product  Designer ", "editor", "PRODUCT DESIGNER"],
                "secondary_roles": ["writer", "researcher", "designer"],
            }
        }
    )
    day = date(2026, 10, 11)
    first = profile_queries(profile, 2, day)
    assert len(first) == 2 and first == profile_queries(profile, 2, day)
    assert first != profile_queries(profile, 2, day + timedelta(days=1))
    rotated = {
        q for delta in range(5) for q in profile_queries(profile, 2, day + timedelta(days=delta))
    }
    assert rotated == {"Product Designer", "editor", "writer", "researcher", "designer"}
    assert profile_queries(Profile(), 3, day) == [None]


@pytest.mark.parametrize(
    "settings",
    [
        {"page_size": 21},
        {"max_queries": 0},
        {"mode": "other"},
        {"country": "Canada"},
        {"worldwide_only": True},
        {"mode": "search", "country": "Canada", "worldwide_only": True},
        {"mode": "search", "country": " "},
    ],
)
def test_settings_reject_unsupported_values(settings):
    with pytest.raises(ValidationError):
        HimalayasSettings(**settings)


def test_default_does_not_enable_himalayas():
    assert not DiscoverySettings().providers.himalayas.enabled


def test_salary_preserves_reported_precision(payload):
    payload["jobs"][0].update(minSalary=123456.78, maxSalary=223456.89)
    result, _ = collect(lambda request: httpx.Response(200, json=payload))
    assert result.jobs[0].salary_text == "123456.78–223456.89 CAD / year"
