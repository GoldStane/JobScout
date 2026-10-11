"""M5: ephemeral index leads, independent publisher verification, bounded HTTP."""

import asyncio
import copy
import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from job_scout.discovery.providers.brave import BraveHTTP, discover_publishers
from job_scout.discovery.queries import ATS_DOMAINS, search_queries
from job_scout.discovery.settings import BraveSettings, DiscoverySettings, select_country
from job_scout.matching.profile import Profile
from job_scout.models import Job
from job_scout.sources.base import BoardHTTP

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH = json.loads((FIXTURES / "brave.json").read_text())
PUBLISHERS = json.loads((FIXTURES / "discovery.json").read_text())
PROFILE = Profile.model_validate({"preferences": {"primary_roles": ["Engineer"]}})


def collect(search=None, publisher=None, *, profile=PROFILE, blocked=None, **options):
    calls, waits, reservations = [], [], []

    async def sleep(delay):
        waits.append(delay)

    async def run():
        settings = BraveSettings(**options)

        def response(request):
            calls.append(request)
            if request.url.host == "api.search.brave.com":
                return search(request) if search else httpx.Response(200, json=SEARCH)
            if publisher:
                return publisher(request)
            source = {
                "api.lever.co": "lever",
                "boards-api.greenhouse.io": "greenhouse",
                "api.ashbyhq.com": "ashby",
            }[request.url.host]
            return httpx.Response(200, json=PUBLISHERS[source])

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            search_http = BraveHTTP(
                client, settings, lambda: reservations.append(True), sleep=sleep
            )
            publisher_http = BoardHTTP(
                client,
                interval=0,
                sleep=sleep,
                attempts=settings.attempts,
                max_requests=settings.publisher_max_requests,
                max_bytes=settings.max_response_bytes,
            )
            result = await discover_publishers(
                search_http, publisher_http, settings, profile, blocked_boards=blocked
            )
            return result

    return asyncio.run(run()), calls, waits, reservations


def test_search_results_never_become_jobs_only_publisher_content_survives():
    result, calls, _, reservations = collect()
    assert result.error is None and result.requests == len(reservations) == 1
    assert result.publisher_requests == 3 and len(result.jobs) == 3
    assert {j.source for j in result.jobs} == {"lever", "greenhouse", "ashby"}
    assert all(j.title in {"Engineering Intern", "Graduate Engineer"} for j in result.jobs)
    assert all(j.posted_at is None for j in result.jobs)
    assert "SEARCH_" not in result.model_dump_json()
    assert len(calls) == 4
    assert all("SEARCH_" not in str(j.application_url) for j in result.jobs)
    raw = result.jobs[0].model_dump() | {"source": "brave"}
    with pytest.raises(ValidationError):
        Job.model_validate(raw)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/board/1",
        "https://127.0.0.1/1",
        "http://169.254.169.254/latest/meta-data",
        "https://jobs.lever.co.evil.test/a/1",
        "https://user:pass@jobs.lever.co/a/1",
        "https://jobs.lever.co/a",
        "file:///etc/passwd",
        "https://jobs.lever.co/a/../1",
        "https://unknown.example/jobs/a",
        "https://jobs.lever.co:444/a/1",
    ],
)
def test_unsafe_or_board_only_leads_are_never_fetched_or_returned(url):
    payload = {"query": {}, "web": {"results": [{"url": url}]}}
    result, calls, _, _ = collect(lambda r: httpx.Response(200, json=payload))
    assert len(calls) == 1 and not result.jobs and not result.sources
    assert result.skipped == 1
    assert url not in result.model_dump_json()


@pytest.mark.parametrize("provider", ["lever", "greenhouse", "ashby"])
def test_publisher_mismatch_is_never_persistable(provider):
    urls = {
        "lever": "https://jobs.lever.co/example/1",
        "greenhouse": "https://boards.greenhouse.io/example/jobs/1",
        "ashby": "https://jobs.ashbyhq.com/example/1",
    }
    bad = copy.deepcopy(PUBLISHERS[provider])
    if provider == "lever":
        bad[0]["hostedUrl"] = "https://jobs.lever.co/different/1"
    elif provider == "greenhouse":
        bad["jobs"][0]["absolute_url"] = "https://boards.greenhouse.io/different/jobs/1"
    else:
        bad["jobs"][0]["applyUrl"] = "http://127.0.0.1/apply"
    result, _, _, _ = collect(
        lambda r: httpx.Response(
            200, json={"query": {}, "web": {"results": [{"url": urls[provider]}]}}
        ),
        lambda r: httpx.Response(200, json=bad),
    )
    assert result.failed_boards == 1 and result.error and not result.jobs
    assert "different" not in result.model_dump_json()


def test_missing_closed_posting_and_duplicates_do_not_create_jobs():
    payload = copy.deepcopy(SEARCH)
    payload["web"]["results"] = [payload["web"]["results"][0]] * 3
    result, _, _, _ = collect(
        lambda r: httpx.Response(200, json=payload), lambda r: httpx.Response(200, json=[])
    )
    assert result.publisher_requests == 1 and not result.jobs
    result, _, _, _ = collect(lambda r: httpx.Response(200, json=payload))
    assert result.publisher_requests == 1 and len(result.jobs) == 1


def test_paging_uses_zero_based_offset_and_explicit_more_flag():
    seen = []

    def search(request):
        seen.append(dict(request.url.params))
        index = int(request.url.params["offset"])
        payload = copy.deepcopy(SEARCH)
        payload["web"]["results"] = [payload["web"]["results"][index]]
        payload["query"]["more_results_available"] = index == 0
        return httpx.Response(200, json=payload)

    result, _, _, _ = collect(search, max_pages=3, results_per_page=1)
    assert [p["offset"] for p in seen] == ["0", "1"]
    assert all(p["count"] == "1" and p["country"] == "ALL" for p in seen)
    assert result.pages == 2 and len(result.jobs) == 2 and not result.limited


def test_page_caps_repeated_pages_and_search_failure_keep_verified_results():
    payload = copy.deepcopy(SEARCH)
    payload["query"]["more_results_available"] = True
    result, _, _, _ = collect(lambda r: httpx.Response(200, json=payload), max_pages=1)
    assert result.limited and len(result.jobs) == 3
    result, _, _, _ = collect(lambda r: httpx.Response(200, json=payload), max_pages=3)
    assert "Repeated" in result.error and len(result.jobs) == 3

    def search(request):
        if request.url.params["offset"] == "1":
            return httpx.Response(403, text="SECRET RESPONSE")
        return httpx.Response(200, json=payload)

    result, _, _, _ = collect(search, max_pages=3)
    assert "403" in result.error and len(result.jobs) == 3
    assert "SECRET" not in result.model_dump_json()


def test_boards_and_publisher_request_budgets():
    result, _, _, _ = collect(max_boards=1)
    assert len(result.jobs) == result.publisher_requests == 1 and result.deferred == 2
    result, _, _, _ = collect(publisher_max_requests=1)
    assert len(result.jobs) == result.publisher_requests == 1 and result.failed_boards == 2


def test_blocked_boards_are_not_verified():
    result, calls, _, _ = collect(blocked={(p, "global", "example") for p in PUBLISHERS})
    assert not result.jobs and len(calls) == 1


@pytest.mark.parametrize(
    "payload", [[], {}, {"query": {}, "web": []}, {"query": {"more_results_available": "true"}}]
)
def test_malformed_search_response_isolated(payload):
    result, calls, _, _ = collect(lambda r: httpx.Response(200, json=payload))
    assert result.error and not result.jobs and len(calls) == 1


def test_empty_results_and_partial_item_shape():
    result, _, _, _ = collect(lambda r: httpx.Response(200, json={"query": {}}))
    assert not result.error and not result.jobs
    payload = copy.deepcopy(SEARCH)
    payload["web"]["results"].append({"description": "SECRET"})
    result, _, _, _ = collect(lambda r: httpx.Response(200, json=payload))
    assert result.skipped == 1 and len(result.jobs) == 3


def test_rate_limit_headers_stop_monthly_quota_and_keep_last_success():
    payload = copy.deepcopy(SEARCH)
    payload["query"]["more_results_available"] = True
    headers = {
        "X-RateLimit-Remaining": "0, 0",
        "X-RateLimit-Reset": "1, 3600",
        "X-RateLimit-Limit": "1, 100",
    }
    result, _, _, reservations = collect(
        lambda r: httpx.Response(200, json=payload, headers=headers), max_pages=3
    )
    assert result.requests == len(reservations) == 1
    assert "quota exhausted" in result.error and len(result.jobs) == 3


def test_retry_requests_are_reserved_and_rate_limit_wait_is_respected():
    count = 0

    def search(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(429, headers={"Retry-After": "4"})
        return httpx.Response(200, json=SEARCH)

    result, _, waits, reservations = collect(search)
    assert len(reservations) == result.requests == 2 and result.error is None
    assert 4 in waits
    result, _, _, reservations = collect(lambda r: httpx.Response(429), max_requests=1)
    assert len(reservations) == 1 and result.requests == 1 and result.error


def test_short_rate_window_and_unlimited_month_are_respected():
    calls = 0

    def search(request):
        nonlocal calls
        calls += 1
        payload = copy.deepcopy(SEARCH)
        payload["web"]["results"] = [payload["web"]["results"][calls - 1]]
        payload["query"]["more_results_available"] = calls == 1
        return httpx.Response(
            200,
            json=payload,
            headers={
                "X-RateLimit-Limit": "1, 0",
                "X-RateLimit-Remaining": "0, 0",
                "X-RateLimit-Reset": "4, 30000",
            },
        )

    result, _, waits, _ = collect(search, max_pages=2)
    assert result.requests == 2 and result.error is None
    assert any(4 <= delay <= 4.2 for delay in waits)


def test_timeout_redirect_and_response_size_are_bounded():
    def timeout(request):
        raise httpx.ReadTimeout("private URL and credential", request=request)

    result, _, _, reservations = collect(timeout, attempts=2)
    assert result.requests == len(reservations) == 2 and "ReadTimeout" in result.error
    assert "private URL" not in result.error
    result, calls, _, _ = collect(
        lambda r: httpx.Response(302, headers={"Location": "http://127.0.0.1"})
    )
    assert "302" in result.error and len(calls) == 1
    result, _, _, _ = collect(
        lambda r: httpx.Response(200, content=b"x" * 2000), max_response_bytes=1024
    )
    assert "size limit" in result.error


def test_no_roles_never_spends_a_search_request():
    result, calls, _, reservations = collect(profile=Profile())
    assert "requires at least one" in result.error
    assert not calls and not reservations


def test_role_country_queries_rotate_without_employers_or_cartesian_product():
    profile = Profile.model_validate(
        {
            "preferences": {
                "primary_roles": ["Designer", "Editor", "Writer", "Researcher"],
                "preferred_regions": ["Canada", "Europe"],
            }
        }
    )
    day = date(2026, 10, 11)
    queries = search_queries(profile, 2, day, "Hong Kong")
    assert len(queries) == 2 and queries == search_queries(profile, 2, day, "Hong Kong")
    assert all('"Hong Kong"' in q and all(f"site:{d}" in q for d in ATS_DOMAINS) for q in queries)
    assert queries != search_queries(profile, 2, day + timedelta(days=1), "Hong Kong")
    rotated = [
        q
        for delta in range(4)
        for q in search_queries(profile, 2, day + timedelta(days=delta), None)
    ]
    for role in ["Designer", "Editor", "Writer", "Researcher"]:
        for place in ["Canada", "Europe"]:
            assert any(f'"{role}" "{place}"' in q for q in rotated)
    assert all(len(q) <= 400 and len(q.split()) <= 50 for q in queries)
    assert search_queries(Profile(), 2, day, None) == []


def test_country_override_does_not_enable_providers_and_accepts_hk_alias():
    settings = DiscoverySettings.model_validate({"providers": {"himalayas": {"enabled": True}}})
    changed = select_country(settings, "HK")
    assert changed.providers.himalayas.mode == "search"
    assert changed.providers.himalayas.country == changed.providers.brave.country == "Hong Kong"
    assert not changed.providers.brave.enabled and settings.providers.himalayas.mode == "browse"
    with pytest.raises(ValueError):
        select_country(settings, "??")


@pytest.mark.parametrize(
    "settings",
    [
        {"max_pages": 11},
        {"max_boards": 0},
        {"daily_request_limit": 0},
        {"monthly_request_limit": -1},
        {"country": " "},
        {"results_per_page": 21},
        {"api_key": "secret"},
        {"max_requests": 0},
    ],
)
def test_invalid_settings_fail_before_network(settings):
    with pytest.raises(ValidationError):
        BraveSettings(**settings)


def test_publisher_runtime_timeout_preserves_already_verified_jobs():
    calls = 0

    async def publisher(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=PUBLISHERS["lever"])
        await asyncio.sleep(0.1)
        return httpx.Response(200, json={"jobs": []})

    result, _, _, _ = collect(publisher=publisher, publisher_runtime_seconds=0.01)
    assert "runtime budget" in result.error and len(result.jobs) == 1
    assert result.publisher_requests == 2


def test_search_runtime_timeout_never_persists_unverified_leads():
    async def search(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, json=SEARCH)

    result, _, _, _ = collect(search, max_runtime_seconds=0.01)
    assert "runtime budget" in result.error and not result.sources
