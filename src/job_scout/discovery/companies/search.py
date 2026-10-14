"""Ephemeral company leads; retain only independently corroborated publisher evidence."""

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from datetime import date

from job_scout.discovery.companies.careers import CareersHTTP, origin
from job_scout.discovery.companies.diagnostics import ACTIONS, STAGES, PageFetchError, classify
from job_scout.discovery.detect import HOSTS, SUPPORTED, DiscoveryError, detect
from job_scout.discovery.providers.brave import search_page
from job_scout.discovery.queries import profile_roles
from job_scout.discovery.validate import Validation, validate
from job_scout.models import utc_now
from job_scout.sources.base import SourceError


def interests(profile, settings) -> list[str]:
    return settings.engineering_domains or profile_roles(profile) + profile.skills


def company_queries(profile, settings, maximum: int, day: date, country=None) -> list[str]:
    domains = list(
        dict.fromkeys(settings.engineering_domains or profile_roles(profile) or profile.skills)
    )
    if not domains:
        return []
    places = [country] if country else profile.preferences.preferred_regions or ["worldwide"]
    week = day.toordinal() // 7
    count = min(maximum, len(domains))
    queries = []
    for index in range(count):
        slot = week * count + index
        domain = " ".join(domains[slot % len(domains)].replace('"', " ").split())[:100]
        place = " ".join(places[(slot // len(domains)) % len(places)].split())[:80]
        queries.append(f'"{domain}" companies {place} official careers')
    return queries


@dataclass
class CompanyReport:
    status: str = "completed"
    queries: int = 0
    sites: int = 0
    requests: int = 0
    site_requests: int = 0
    publisher_requests: int = 0
    registered: int = 0
    pending_review: int = 0
    unsupported: int = 0
    skipped: int = 0
    failed: int = 0
    limited: bool = False
    error: str | None = None
    quota: dict | None = None
    outcomes: list[dict] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)
    next_due: str | None = None
    diagnostics_omitted: int = 0

    def note(self, stage, code, *, site=None, query=None, http_status=None):
        if stage == "search" and code == "http_denied":
            code = "search_denied"
        assert stage in STAGES and code in ACTIONS
        item = {"stage": stage, "code": code, "action": ACTIONS[code]}
        if site is not None:
            item["site"] = site
        if query is not None:
            item["query"] = query
        if http_status is not None:
            item["http_status"] = http_status
        if len(self.diagnostics) >= 200:
            self.diagnostics_omitted += 1
            return
        self.diagnostics.append(item)
        logging.getLogger(__name__).info(
            "company_discovery query=%s site=%s stage=%s code=%s http_status=%s action=%s",
            query,
            site,
            stage,
            code,
            http_status,
            ACTIONS[code],
        )

    def as_dict(self):
        return asdict(self)


async def discover_companies(search_http, site_http, publisher_http, registry, profile, config):
    settings, search_settings = config.companies, config.providers.brave
    report = CompanyReport()
    pages = CareersHTTP(site_http, max_redirects=settings.max_redirects)
    seen_sites, seen_boards = set(), set()
    known = {row["url"] for row in registry.candidates() if row["state"] == "rejected"}
    known.update(row["board_url"] for row in registry.sources())

    stage = "search"
    current_query = 0
    deferred = []

    async def inspect_site(site):
        nonlocal stage
        stage = "homepage"
        homepage = await pages.page(site + "/")
        identities = set(homepage.organizations)
        branding_identity = not identities
        if branding_identity:
            identities = {(name, site) for name in homepage.site_names}
        if len(identities) != 1:
            report.pending_review += 1
            report.note(
                "identity",
                "identity_missing" if not identities else "identity_ambiguous",
                site=report.sites,
                query=current_query,
            )
            return  # No uncorroborated search URL is retained.
        name, website = identities.pop()
        evidence_pages = [homepage]
        for url, label in homepage.links:
            if len(evidence_pages) > settings.max_careers_pages:
                break
            try:
                if (
                    origin(url) == website
                    and any(word in (label + " " + url).casefold() for word in ("careers", "jobs"))
                    and url not in {page.url for page in evidence_pages}
                ):
                    stage = "careers"
                    evidence_pages.append(await pages.page(url))
            except DiscoveryError:
                continue
        content = " ".join(" ".join(page.text) for page in evidence_pages).casefold()
        matched = [term for term in interests(profile, settings) if term.casefold() in content]
        if not matched:
            report.note("relevance", "irrelevant", site=report.sites, query=current_query)
            report.skipped += 1
            return
        found = False
        for page in evidence_pages:
            for url, _ in page.links:
                try:
                    candidate = detect(url, name=name)
                except DiscoveryError:
                    continue
                if candidate.config is None or candidate.config.source not in SUPPORTED:
                    continue
                found = True
                if candidate.identity in seen_boards or candidate.url in known:
                    report.note(
                        "registration", "already_known", site=report.sites, query=current_query
                    )
                    continue
                if len(seen_boards) >= search_settings.max_boards:
                    report.limited = True
                    report.note("limits", "board_limit", site=report.sites, query=current_query)
                    return
                seen_boards.add(candidate.identity)
                # The ATS-hosted page must independently link back to this website.
                stage = "ats_page"
                board_page = await pages.page(candidate.url)
                reciprocal = False
                for backlink, _ in board_page.links:
                    try:
                        reciprocal |= origin(backlink) == website
                    except DiscoveryError:
                        pass
                if branding_identity:
                    # A publisher site name alone is not an employer identity. Require
                    # the same name independently on the ATS page plus its backlink.
                    reciprocal &= {name.casefold()} == {
                        value.casefold() for value in board_page.site_names
                    }
                if not reciprocal:
                    report.note(
                        "ats_identity",
                        "reciprocal_link_missing",
                        site=report.sites,
                        query=current_query,
                    )
                    report.pending_review += 1
                    continue
                stage = "ats_api"
                verdict = await validate(candidate, publisher_http.client, http=publisher_http)
                if verdict.endpoint_valid and (
                    verdict.state == "verified" or verdict.jobs_found == 0
                ):
                    verdict = Validation(
                        "verified",
                        "Official organization and reciprocal careers links verified; "
                        "public ATS endpoint validated",
                        True,
                        verdict.jobs_found,
                    )
                # Only publisher-derived identities, links and configured criteria persist.
                stage = "registration"
                saved = registry.record(
                    candidate,
                    verdict,
                    method="company_search",
                    official_website=website,
                    evidence={
                        "official_website": website,
                        "careers_page": page.url,
                        "reciprocal_board_link": True,
                        "identity_method": "shared_site_name"
                        if branding_identity
                        else "organization",
                        "criteria": matched[:10],
                    },
                )
                report.outcomes.append(
                    {"company": name, "board": candidate.url, "state": saved["state"]}
                )
                if saved["state"] == "active":
                    report.registered += 1
                    report.note(
                        "registration", "registered", site=report.sites, query=current_query
                    )
                else:
                    report.pending_review += 1
                    if verdict.state == "pending_validation":
                        report.failed += 1
                    diagnostic = classify(SourceError(verdict.reason))
                    if diagnostic["code"] == "verification_error":
                        diagnostic["code"] = "validation_failed"
                    report.note("ats_api", site=report.sites, query=current_query, **diagnostic)
        if not found:
            report.unsupported += 1
            report.note("careers", "unsupported", site=report.sites, query=current_query)

    async def inspect_lead(url):
        nonlocal stage
        try:
            stage = "lead"
            site = origin(url)
            if site in seen_sites or site.removeprefix("https://") in HOSTS:
                return
            seen_sites.add(site)
            report.sites += 1
            await inspect_site(site)
        except DiscoveryError as exc:
            report.skipped += 1
            report.note(
                stage,
                classify(exc)["code"],
                query=current_query,
                site=report.sites,
            )
        except SourceError as exc:
            report.failed += 1
            diagnostic = exc.diagnostic if isinstance(exc, PageFetchError) else classify(exc)
            report.note(
                exc.stage if isinstance(exc, PageFetchError) else stage,
                site=report.sites,
                query=current_query,
                **diagnostic,
            )

    try:
        async with asyncio.timeout(search_settings.max_runtime_seconds):
            queries = company_queries(
                profile,
                settings,
                search_settings.max_queries,
                utc_now().date(),
                search_settings.country,
            )
            if not queries:
                report.note("search", "invalid_search")
                raise SourceError(
                    "Company discovery needs engineering domains or profile interests"
                )
            for query_index, query in enumerate(queries):
                if report.sites >= settings.max_sites:
                    report.limited = True
                    report.note("limits", "site_limit", query=current_query)
                    break
                remaining_queries = len(queries) - query_index
                allowance = (
                    settings.max_sites - report.sites + remaining_queries - 1
                ) // remaining_queries
                query_site_limit = report.sites + allowance
                report.queries += 1
                current_query = report.queries
                query_urls = set()
                for offset in range(search_settings.max_pages):
                    stage = "search"
                    try:
                        urls, more, skipped = await search_page(
                            search_http, query, offset, search_settings
                        )
                    except SourceError as exc:
                        report.note("search", query=current_query, **classify(exc))
                        report.error = (
                            "Company search stopped; fetched candidates were still processed"
                        )
                        report.quota = getattr(exc, "quota", None) or getattr(
                            search_http, "quota", None
                        )
                        if (
                            report.quota is None
                            and search_http.requests >= search_http.max_requests
                        ):
                            report.quota = {
                                "scope": "per_run",
                                "used": search_http.requests,
                                "limit": search_http.max_requests,
                                "reset_at": None,
                            }
                        report.limited |= classify(exc)["code"] in {
                            "request_budget",
                            "http_rate_limited",
                        }
                        break
                    report.skipped += skipped
                    fresh = set(urls) - query_urls
                    query_urls.update(urls)
                    for url in urls:
                        if report.sites >= query_site_limit:
                            deferred.append((current_query, url))
                            continue
                        await inspect_lead(url)
                    if report.sites >= query_site_limit:
                        break
                    if not more or not urls:
                        break
                    if not fresh:
                        report.note("search", "repeated_page", query=current_query)
                        raise SourceError("Repeated company-search result page")
                else:
                    report.limited = True
                    report.note("limits", "page_limit", query=current_query)
                if report.error:
                    break
            # Spend unused shares on transient leads already returned by the API.
            # No extra search requests and no persisted search-result queue.
            for lead_query, url in deferred:
                try:
                    if origin(url) in seen_sites:
                        continue
                except DiscoveryError:
                    continue
                if report.sites >= settings.max_sites:
                    report.limited = True
                    report.note("limits", "site_limit", query=lead_query)
                    break
                current_query = lead_query
                await inspect_lead(url)
    except SourceError as exc:
        report.note(stage, query=current_query, **classify(exc))
        report.error = "Company search unavailable or request budget exhausted"
    except TimeoutError:
        report.note(stage, "timeout", query=current_query, site=report.sites or None)
        report.error = "Company discovery runtime budget exhausted"
    finally:
        report.requests = search_http.requests
        report.site_requests = site_http.requests
        report.publisher_requests = publisher_http.requests
        if report.error and not report.registered:
            report.status = "failed"
        elif report.failed or report.error or report.limited:
            report.status = "partial"
        elif not report.registered:
            report.status = "no_new_boards"
    return report
