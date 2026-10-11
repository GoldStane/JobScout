"""Opportunistic board registration from supplied posting links, never crawling."""

import asyncio
from dataclasses import asdict, dataclass, field
from html import unescape
from html.parser import HTMLParser

import httpx

from job_scout.discovery.base import DiscoveryBatch, DiscoveryResult
from job_scout.discovery.detect import SUPPORTED, Candidate, DiscoveryError, detect
from job_scout.discovery.posting_links import canonical_posting_url
from job_scout.discovery.registry import Registry
from job_scout.discovery.settings import RegistrationSettings
from job_scout.discovery.validate import Validation, validate
from job_scout.sources.base import BoardHTTP


def ats_links(description: str, *, postings: bool = False) -> list[str]:
    """Keep at most ten recognized board leads from actual visible HTML anchors.

    URLs are canonicalized before retention; tracking parameters and credentials
    are never stored. A link is a board lead, not proof of a direct application.
    """

    class Links(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.urls: list[str] = []
            self.hidden: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag in {"script", "style", "template"}:
                self.hidden.append(tag)
            if tag != "a" or self.hidden or len(self.urls) >= 10:
                return
            for key, value in attrs:
                if key != "href" or not value:
                    continue
                try:
                    candidate = detect(value)
                except DiscoveryError:
                    continue
                if candidate.config and candidate.config.source in SUPPORTED:
                    url = canonical_posting_url(value) if postings else candidate.url
                    if url and url not in self.urls:
                        self.urls.append(url)

        def handle_endtag(self, tag):
            if self.hidden and self.hidden[-1] == tag:
                self.hidden.pop()

    parser = Links()
    for _ in range(2):
        description = unescape(description)
    parser.feed(description)
    parser.close()
    return parser.urls


@dataclass
class RegistrationReport:
    outcomes: list[dict] = field(default_factory=list)
    requests: int = 0
    deferred: int = 0
    error: str | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None or any(
            outcome["state"] == "pending_validation" for outcome in self.outcomes
        )

    def as_dict(self) -> dict:
        return asdict(self)


async def register_discovered(
    result: DiscoveryResult | DiscoveryBatch,
    registry: Registry,
    client: httpx.AsyncClient,
    settings: RegistrationSettings,
    *,
    dry_run: bool = False,
) -> RegistrationReport:
    report = RegistrationReport()
    if not settings.enabled:
        return report
    # Feed identity and ATS board identity deliberately remain separate.
    leads: dict[str, tuple[Candidate, list[dict]]] = {}
    for job in result.jobs:
        urls = job.source_metadata.get("ats_board_leads", [])
        if not isinstance(urls, list):
            continue
        for url in urls[:10]:
            if not isinstance(url, str):
                continue
            try:
                candidate = detect(url)
            except (DiscoveryError, TypeError):
                continue
            if candidate.config is None or candidate.config.source not in SUPPORTED:
                continue
            if candidate.identity not in leads:
                leads[candidate.identity] = (candidate, [])
            evidence = leads[candidate.identity][1]
            if len(evidence) < 10:
                evidence.append(
                    {
                        "provider": job.source,
                        "posting_id": job.source_job_id,
                        "listing_url": str(job.source_url),
                    }
                )
    known = {
        (row["provider"], row["region"], row["board_id"]): row
        for row in registry.sources()
        if row["status"] in {"active", "disabled"}
    }
    rejected = {row["url"] for row in registry.candidates() if row["state"] == "rejected"}
    pending = []
    for candidate, evidence in leads.values():
        config = candidate.config
        assert config is not None
        key = (config.source, config.region, config.board_id)
        if key in known or candidate.url in rejected:
            report.outcomes.append(
                {
                    "board": candidate.url,
                    "state": "rejected" if candidate.url in rejected else known[key]["status"],
                    "validation": "skipped_existing",
                }
            )
        else:
            pending.append((candidate, evidence))
    http = BoardHTTP(
        client,
        max_requests=settings.max_requests,
        max_bytes=settings.max_response_bytes,
        interval=settings.interval_seconds,
    )
    completed = 0
    try:
        async with asyncio.timeout(settings.max_runtime_seconds):
            for candidate, evidence in pending[: settings.max_new_boards_per_run]:
                if http.requests >= settings.max_requests:
                    break
                verdict = await validate(candidate, client, http=http)
                if not dry_run:
                    saved = registry.record(
                        candidate,
                        verdict,
                        method="global_discovery",
                        evidence={"postings": evidence},
                    )
                    state = saved["state"]
                else:
                    state = verdict.state
                report.outcomes.append(
                    {
                        "board": candidate.url,
                        "state": state,
                        "reason": verdict.reason,
                        "validation": "attempted",
                    }
                )
                completed += 1
    except TimeoutError:
        report.error = "ATS registration runtime budget exhausted"
        # The current candidate remains retryable and is never falsely verified.
        if not dry_run:
            registry.record(
                candidate,
                Validation("pending_validation", report.error),
                method="global_discovery",
                evidence={"postings": evidence},
            )
    report.requests = http.requests
    report.deferred = len(pending) - completed
    return report
