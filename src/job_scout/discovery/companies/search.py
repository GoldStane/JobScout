"""Ephemeral company leads; retain only independently corroborated publisher evidence."""

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import date

from job_scout.discovery.companies.careers import CareersHTTP, origin
from job_scout.discovery.detect import HOSTS, SUPPORTED, DiscoveryError, detect
from job_scout.discovery.providers.brave import search_page
from job_scout.discovery.queries import profile_roles
from job_scout.discovery.validate import Validation, validate
from job_scout.models import utc_now
from job_scout.sources.base import SourceError


def interests(profile, settings) -> list[str]:
    return settings.engineering_domains or profile_roles(profile) + profile.skills


def company_queries(profile, settings, maximum: int, day: date, country=None) -> list[str]:
    domains = list(dict.fromkeys(interests(profile, settings)))
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
    outcomes: list[dict] = field(default_factory=list)

    def as_dict(self):
        return asdict(self)


async def discover_companies(search_http, site_http, publisher_http, registry, profile, config):
    settings, search_settings = config.companies, config.providers.brave
    report = CompanyReport()
    pages = CareersHTTP(site_http)
    seen_sites, seen_boards = set(), set()
    known = {row["url"] for row in registry.candidates() if row["state"] == "rejected"}
    known.update(row["board_url"] for row in registry.sources())

    async def inspect_site(site):
        homepage = await pages.page(site + "/")
        identities = set(homepage.organizations)
        if len(identities) != 1:
            report.pending_review += 1
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
                    evidence_pages.append(await pages.page(url))
            except DiscoveryError:
                continue
        content = " ".join(" ".join(page.text) for page in evidence_pages).casefold()
        matched = [term for term in interests(profile, settings) if term.casefold() in content]
        if not matched:
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
                    continue
                if len(seen_boards) >= search_settings.max_boards:
                    report.limited = True
                    return
                seen_boards.add(candidate.identity)
                # The ATS-hosted page must independently link back to this website.
                board_page = await pages.page(candidate.url)
                reciprocal = False
                for backlink, _ in board_page.links:
                    try:
                        reciprocal |= origin(backlink) == website
                    except DiscoveryError:
                        pass
                if not reciprocal:
                    report.pending_review += 1
                    continue
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
                saved = registry.record(
                    candidate,
                    verdict,
                    method="company_search",
                    official_website=website,
                    evidence={
                        "official_website": website,
                        "careers_page": page.url,
                        "reciprocal_board_link": True,
                        "criteria": matched[:10],
                    },
                )
                report.outcomes.append(
                    {"company": name, "board": candidate.url, "state": saved["state"]}
                )
                if saved["state"] == "active":
                    report.registered += 1
                else:
                    report.pending_review += 1
        if not found:
            report.unsupported += 1

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
                raise SourceError(
                    "Company discovery needs engineering domains or profile interests"
                )
            for query in queries:
                report.queries += 1
                query_urls = set()
                for offset in range(search_settings.max_pages):
                    urls, more, skipped = await search_page(
                        search_http, query, offset, search_settings
                    )
                    report.skipped += skipped
                    fresh = set(urls) - query_urls
                    query_urls.update(urls)
                    for url in urls:
                        if report.sites >= settings.max_sites:
                            report.limited = True
                            return report
                        try:
                            site = origin(url)
                            if site in seen_sites or site.removeprefix("https://") in HOSTS:
                                continue
                            seen_sites.add(site)
                            report.sites += 1
                            await inspect_site(site)
                        except DiscoveryError:
                            report.skipped += 1
                        except SourceError:
                            # Neither search URLs nor arbitrary publisher errors reach storage/logs.
                            report.failed += 1
                    if not more or not urls:
                        break
                    if not fresh:
                        raise SourceError("Repeated company-search result page")
                else:
                    report.limited = True
    except SourceError:
        report.error = "Company search unavailable or request budget exhausted"
    except TimeoutError:
        report.error = "Company discovery runtime budget exhausted"
    finally:
        report.requests = search_http.requests
        report.site_requests = site_http.requests
        report.publisher_requests = publisher_http.requests
    return report
