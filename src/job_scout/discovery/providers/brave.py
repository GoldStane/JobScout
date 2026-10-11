"""Opt-in web-index leads. Only re-fetched, verified ATS data leaves this module."""

import asyncio
import math
import time
from collections.abc import Callable

import httpx
from pydantic import ValidationError

from job_scout.discovery.base import PublisherDiscoveryResult
from job_scout.discovery.detect import detect
from job_scout.discovery.posting_links import posting_identity
from job_scout.discovery.queries import search_queries
from job_scout.discovery.settings import BraveSettings
from job_scout.matching.profile import Profile
from job_scout.models import SourceResult, utc_now
from job_scout.sources.ashby import AshbySource
from job_scout.sources.base import BoardHTTP, SourceError
from job_scout.sources.greenhouse import GreenhouseSource
from job_scout.sources.lever import LeverSource

ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


class BraveHTTP(BoardHTTP):
    """Honor all reported quota windows without retaining response bodies/headers."""

    def __init__(self, client, settings: BraveSettings, reserve: Callable[[], None], **kwargs):
        self.blocked = False
        self.reserve = reserve
        super().__init__(
            client,
            attempts=settings.attempts,
            interval=settings.interval_seconds,
            max_requests=settings.max_requests,
            max_bytes=settings.max_response_bytes,
            before_request=self.reserve_request,
            after_response=self.rate_limits,
            **kwargs,
        )

    def reserve_request(self):
        if self.blocked:
            raise SourceError("Brave server quota exhausted; retry after the quota reset")
        self.reserve()

    def rate_limits(self, response: httpx.Response):
        if response.status_code in {401, 403}:
            raise SourceError(f"Brave HTTP {response.status_code}: check API key and plan access")
        if response.status_code == 402:
            raise SourceError("Brave HTTP 402: check account quota and plan access")
        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining is None:
            return
        try:
            counts = [int(v.strip()) for v in remaining.split(",")]
            resets = [
                float(v.strip()) for v in response.headers.get("X-RateLimit-Reset", "").split(",")
            ]
            limits = [
                int(v.strip()) for v in response.headers.get("X-RateLimit-Limit", "").split(",")
            ]
            if len(counts) != len(resets) or len(counts) != len(limits):
                raise ValueError("Mismatched quota windows")
            for count, reset, limit in zip(counts, resets, limits, strict=True):
                if not math.isfinite(reset) or reset < 0:
                    raise ValueError("Invalid reset")
                if count <= 0 and limit != 0:  # A zero limit denotes an unlimited window.
                    if reset > 60:
                        self.blocked = True
                    else:
                        self._next_request = max(self._next_request, time.monotonic() + reset + 0.1)
        except ValueError:
            self.blocked = True  # Never guess away an ambiguous server quota.


async def search_page(http: BoardHTTP, query: str, offset: int, settings: BraveSettings):
    params = {
        "q": query,
        "country": "ALL",
        "count": settings.results_per_page,
        "offset": offset,
        "result_filter": "web",
        "text_decorations": "false",
        "spellcheck": "false",
    }
    if settings.freshness:
        params["freshness"] = settings.freshness
    payload = await http.get_json(ENDPOINT, params)
    if not isinstance(payload, dict) or not isinstance(payload.get("query"), dict):
        raise SourceError("Invalid Brave response envelope")
    more = payload["query"].get("more_results_available", False)
    web = payload.get("web")
    results = [] if web is None else web.get("results") if isinstance(web, dict) else None
    if type(more) is not bool or not isinstance(results, list):
        raise SourceError("Invalid Brave web results or pagination")
    urls, skipped = [], 0
    for item in results[: settings.results_per_page]:
        if isinstance(item, dict) and isinstance(item.get("url"), str):
            urls.append(item["url"])
        else:
            skipped += 1
    # Titles, snippets, dates, rankings and original response objects never escape.
    return urls, more, skipped


async def discover_publishers(
    search_http: BoardHTTP,
    publisher_http: BoardHTTP,
    settings: BraveSettings,
    profile: Profile,
    *,
    blocked_boards: set[tuple[str, str, str]] | None = None,
    known_companies: dict[tuple[str, str, str], str] | None = None,
) -> PublisherDiscoveryResult:
    result = PublisherDiscoveryResult(attempted_at=utc_now(), completed_at=utc_now())
    blocked_boards = blocked_boards or set()
    known_companies = known_companies or {}
    # All lead-derived identities remain local and ephemeral until publisher corroboration.
    leads: dict[tuple[str, str, str], dict] = {}
    seen = set()

    async def search():
        queries = search_queries(profile, settings.max_queries, utc_now().date(), settings.country)
        if not queries:
            raise SourceError("Brave search requires at least one primary or secondary role")
        for query in queries:
            result.queries += 1
            query_seen = set()
            for offset in range(settings.max_pages):
                urls, more, invalid = await search_page(search_http, query, offset, settings)
                result.pages += 1
                result.skipped += invalid
                result.leads_seen += len(urls)
                new_page = set(urls) - query_seen
                for url in urls:
                    identity = posting_identity(url)
                    if identity is None:
                        result.skipped += 1
                        continue
                    if identity in seen:
                        continue
                    seen.add(identity)
                    board = identity[:3]
                    if board in blocked_boards:
                        result.skipped += 1
                        continue
                    if board not in leads:
                        if len(leads) >= settings.max_boards:
                            result.deferred += 1
                            continue
                        leads[board] = {"candidate": detect(url), "ids": set()}
                    leads[board]["ids"].add(identity)
                if not more or not urls:
                    break
                if not new_page:
                    raise SourceError("Repeated Brave result page; pagination stopped")
                query_seen.update(urls)
            else:
                result.limited = True

    async def verify():
        adapters = {
            "lever": LeverSource(publisher_http),
            "greenhouse": GreenhouseSource(publisher_http),
            "ashby": AshbySource(publisher_http),
        }
        for lead in leads.values():
            candidate = lead["candidate"]
            config = candidate.config
            company = known_companies.get((config.source, config.region, config.board_id))
            if company:
                config = config.model_copy(update={"company": company})
            attempted = utc_now()
            try:
                jobs = await adapters[config.source].fetch_jobs(config)
                # Validate an entire board before trusting the selected occurrences.
                if len({job.source_job_id for job in jobs}) != len(jobs):
                    raise SourceError("Duplicate publisher identifiers")
                selected = []
                for job in jobs:
                    identity = (
                        job.source,
                        job.source_region,
                        job.company_identifier,
                        job.source_job_id,
                    )
                    if (
                        posting_identity(str(job.source_url)) != identity
                        or posting_identity(str(job.application_url)) != identity
                    ):
                        raise SourceError("Publisher links do not confirm posting identity")
                    if identity in lead["ids"]:
                        selected.append(job)
                if not selected:
                    result.skipped += len(lead["ids"])
                    continue
                # Rebuild registration from the publisher's URL, never the search URL.
                verified = detect(str(selected[0].source_url))
                result.sources.append(
                    SourceResult(
                        config=verified.config.model_copy(update={"company": config.company}),
                        attempted_at=attempted,
                        completed_at=utc_now(),
                        jobs=selected,
                    )
                )
            except (SourceError, ValidationError, ValueError):
                # No unverified board ID/URL, snippets or publisher payload in errors/audits.
                result.failed_boards += 1

    try:
        async with asyncio.timeout(settings.max_runtime_seconds):
            try:
                await search()
            except (SourceError, ValueError) as exc:
                result.error = (
                    str(exc)
                    if isinstance(exc, SourceError)
                    else "Invalid role/location query settings"
                )
            try:
                async with asyncio.timeout(settings.publisher_runtime_seconds):
                    await verify()
            except TimeoutError:
                result.error = "Publisher verification runtime budget exhausted"
    except TimeoutError:
        result.error = "Brave discovery runtime budget exhausted"
    if result.failed_boards:
        result.error = (result.error + "; " if result.error else "") + (
            f"{result.failed_boards} publisher board verification(s) failed"
        )
    result.requests, result.publisher_requests = search_http.requests, publisher_http.requests
    result.completed_at = utc_now()
    return result
