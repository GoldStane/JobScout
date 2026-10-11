"""No-employer-list discovery command with shared matching and digest preview."""

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import httpx
import yaml

from job_scout.discovery.base import DiscoveryBatch
from job_scout.discovery.http import PublicAPITransport
from job_scout.discovery.orchestrator import discover
from job_scout.discovery.output import render_discovery
from job_scout.discovery.providers.arbeitnow import ArbeitnowProvider
from job_scout.discovery.providers.himalayas import HimalayasProvider
from job_scout.discovery.queries import profile_queries
from job_scout.discovery.registration import register_discovered
from job_scout.discovery.registry import Registry
from job_scout.discovery.settings import load_settings
from job_scout.matching.profile import load_profile
from job_scout.matching.scoring import rank_jobs
from job_scout.models import utc_now
from job_scout.notifications.digest import build_digest
from job_scout.sources.base import BoardHTTP
from job_scout.storage.database import open_database
from job_scout.storage.repository import Repository


async def fetch_discovery(configuration):
    settings = configuration.providers.arbeitnow
    if not settings.enabled:
        return None, 0
    async with httpx.AsyncClient(
        transport=PublicAPITransport(allowed_hosts={"www.arbeitnow.com"}),
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": "JobScout/0.1", "Accept": "application/json"},
    ) as client:
        http = BoardHTTP(
            client,
            attempts=settings.attempts,
            interval=settings.interval_seconds,
            max_bytes=settings.max_response_bytes,
            max_requests=settings.max_requests,
        )
        result = await discover(ArbeitnowProvider(http), settings)
        result.requests = http.requests
        return result, http.requests


async def fetch_himalayas(configuration, profile):
    settings = configuration.providers.himalayas
    if not settings.enabled:
        return None, 0
    async with httpx.AsyncClient(
        transport=PublicAPITransport(allowed_hosts={"himalayas.app"}),
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": "JobScout/0.1", "Accept": "application/json"},
    ) as client:
        http = BoardHTTP(
            client,
            attempts=settings.attempts,
            interval=settings.interval_seconds,
            max_bytes=settings.max_response_bytes,
            max_requests=settings.max_requests,
            minimum_rate_limit_delay=60,
        )
        queries = (
            profile_queries(profile, settings.max_queries, utc_now().date())
            if settings.mode == "search"
            else [None]
        )
        result = await discover(HimalayasProvider(http, settings), settings, queries=queries)
        result.requests = http.requests
        return result, http.requests


async def fetch_providers(configuration, profile):
    responses = await asyncio.gather(
        fetch_discovery(configuration), fetch_himalayas(configuration, profile)
    )
    results = [result for result, _ in responses if result is not None]
    return (
        DiscoveryBatch(results=results) if results else None,
        sum(requests for _, requests in responses),
    )


async def register_result(result, engine, configuration, *, dry_run):
    async with httpx.AsyncClient(
        transport=PublicAPITransport(),
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": "JobScout/0.1", "Accept": "application/json"},
    ) as client:
        return await register_discovered(
            result,
            Registry(engine),
            client,
            configuration.registration,
            dry_run=dry_run,
        )


def add_command(subcommands) -> None:
    command = subcommands.add_parser(
        "discover-jobs", help="Find jobs across unknown employers using enabled free job feeds"
    )
    command.add_argument("--config", type=Path, help="Discovery YAML; bounded defaults if omitted")
    command.add_argument("--profile", type=Path, default=Path("config/profile.yaml"))
    command.add_argument("--track", help="Optional career track in the matching profile")
    command.add_argument("--dry-run", action="store_true", help="Fetch and rank without saving")
    command.add_argument("--format", choices=["text", "json"], default="text")
    command.add_argument(
        "--limit", type=int, help="Maximum matches shown; defaults to profile limit"
    )


def execute(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        configuration = load_settings(args.config)
        profile = load_profile(args.profile).for_track(args.track)
        if args.limit is not None:
            if not 1 <= args.limit <= 1000:
                raise ValueError("Limit must be between 1 and 1000")
            profile.digest.maximum_matches = args.limit
    except (OSError, ValueError, yaml.YAMLError) as exc:
        parser.exit(2, f"Cannot load discovery settings or profile ({type(exc).__name__}).\n")
    if not any(
        p.enabled for p in (configuration.providers.arbeitnow, configuration.providers.himalayas)
    ):
        report = {"status": "disabled"}
        print(json.dumps(report) if args.format == "json" else render_discovery(report))
        return 0
    engine = open_database()
    try:
        repository = Repository(engine)

        batch, requests = asyncio.run(fetch_providers(configuration, profile))
        assert batch is not None
        counts = None
        provider_counts = {}
        if not args.dry_run:
            for result in batch.results:
                current = asdict(repository.ingest_discovery(result))
                provider_counts[result.provider] = current
                counts = {key: (counts or {}).get(key, 0) + value for key, value in current.items()}

        registration = asyncio.run(
            register_result(batch, engine, configuration, dry_run=args.dry_run)
        )
        # Rank only this run's observations; preserve first-seen dates for shared sorting.
        stored = {(job.source, job.source_job_id): job for job in repository.jobs()}
        jobs = [
            job.model_copy(
                update={
                    "first_seen_at": stored.get((job.source, job.source_job_id), job).first_seen_at
                }
            )
            for job in batch.jobs
        ]
        matches = [m for m in rank_jobs(jobs, profile) if m.score >= profile.digest.minimum_score]
        failures = []
        for result in batch.results:
            if result.error:
                failures.append(f"{result.provider}: {result.error}")
            if result.skipped:
                failures.append(f"{result.provider}: skipped {result.skipped} invalid postings")
        previews = build_digest(
            matches,
            now=max(result.completed_at for result in batch.results),
            timezone=profile.digest.timezone,
            failures=failures,
            successful_sources=sum(result.pages > 0 for result in batch.results),
            maximum_matches=profile.digest.maximum_matches,
            discovery_preview=True,
        )
        output = {
            "provider": ", ".join(result.provider for result in batch.results),
            "pages": sum(result.pages for result in batch.results),
            "requests": requests,
            "fetched_jobs": len(jobs),
            "skipped_jobs": sum(result.skipped for result in batch.results),
            "expired_jobs": sum(result.expired for result in batch.results),
            "page_limit_reached": any(result.limited for result in batch.results),
            "error": "; ".join(r.error for r in batch.results if r.error) or None,
            "dry_run": args.dry_run,
            "ingestion": counts,
            "matching_jobs": len(matches),
            "ats_registration": registration.as_dict(),
            # Preview only: no notification records or claims of delivery.
            "preview_assumes_undelivered": True,
            "digest_preview": [part.text for part in previews],
        }
        output["providers"] = [
            {
                "provider": r.provider,
                "pages": r.pages,
                "requests": r.requests,
                "queries": r.queries,
                "unique_jobs": len(r.jobs),
                "skipped": r.skipped,
                "expired": r.expired,
                "error": r.error,
                "page_limit_reached": r.limited,
                "matching_jobs": sum(m.job.source == r.provider for m in matches),
                "ingestion": provider_counts.get(r.provider),
            }
            for r in batch.results
        ]
        output["matches"] = [
            match.model_dump(
                mode="json",
                exclude={"job": {"description_text", "source_metadata", "eligibility_information"}},
            )
            for match in matches[: profile.digest.maximum_matches]
        ]
        print(
            json.dumps(output, indent=2, ensure_ascii=False)
            if args.format == "json"
            else render_discovery(output)
        )
        return 1 if failures or registration.failed else 0
    finally:
        engine.dispose()
