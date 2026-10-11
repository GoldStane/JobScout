"""Verify candidates with existing adapters, never crawl arbitrary URLs."""

import asyncio
from dataclasses import dataclass

import httpx
from pydantic import ValidationError

from job_scout.discovery.detect import SUPPORTED, Candidate, DiscoveryError, detect
from job_scout.sources.ashby import AshbySource
from job_scout.sources.base import BoardHTTP, SourceError
from job_scout.sources.greenhouse import GreenhouseSource
from job_scout.sources.lever import LeverSource


@dataclass(frozen=True)
class Validation:
    state: str
    reason: str
    endpoint_valid: bool = False
    jobs_found: int = 0


async def validate(
    candidate: Candidate,
    client: httpx.AsyncClient,
    *,
    max_requests: int = 12,
    max_bytes: int = 2_000_000,
    interval: float = 0.5,
    http: BoardHTTP | None = None,
) -> Validation:
    config = candidate.config
    if config is None or config.source not in SUPPORTED:
        return Validation("needs_review", "Unsupported careers URL/provider; no HTTP request made")
    # Endpoints are constructed ONLY by existing adapters from validated slugs.
    # No input URL, returned posting URL, or redirect is fetched. Zero redirects.
    http = http or BoardHTTP(
        client, max_requests=max_requests, max_bytes=max_bytes, interval=interval
    )
    adapters = {
        "greenhouse": GreenhouseSource(http),
        "lever": LeverSource(http),
        "ashby": AshbySource(http),
    }
    try:
        async with asyncio.timeout(120):
            jobs = await adapters[config.source].fetch_jobs(config)
    except TimeoutError:
        return Validation("pending_validation", "Validation time budget exhausted; retry later")
    except (SourceError, ValidationError) as exc:
        # Adapter errors are controlled strings. Pydantic errors may include job payloads.
        reason = str(exc) if isinstance(exc, SourceError) else "Malformed canonical job data"
        return Validation("pending_validation", reason)
    if len({job.source_job_id for job in jobs}) != len(jobs):
        return Validation("pending_validation", "Duplicate posting IDs in response")
    if not jobs:
        return Validation(
            "needs_review", "Valid empty board; employer identity needs manual confirmation", True
        )
    for job in jobs:
        try:
            linked = detect(str(job.source_url))
        except DiscoveryError:
            linked = None
        if linked is None or linked.identity != candidate.identity:
            return Validation(
                "needs_review",
                "Posting links do not unambiguously confirm this board; review identity",
                True,
                len(jobs),
            )
    return Validation(
        "verified",
        "Public endpoint validated; posting URLs agree with supplied board",
        True,
        len(jobs),
    )
