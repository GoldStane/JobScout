"""No-employer-list discovery command with shared matching and digest preview."""

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

import httpx
import yaml

from job_scout.deduplicate import reconcile_jobs
from job_scout.discovery import http as public_http
from job_scout.discovery.base import (
    DiscoveredJob,
    DiscoveryBatch,
    DiscoveryResult,
    PublisherDiscoveryResult,
)
from job_scout.discovery.budgets import SearchBudget
from job_scout.discovery.detect import detect
from job_scout.discovery.orchestrator import discover
from job_scout.discovery.output import render_discovery
from job_scout.discovery.providers.arbeitnow import ArbeitnowProvider
from job_scout.discovery.providers.brave import BraveHTTP, discover_publishers
from job_scout.discovery.providers.himalayas import HimalayasProvider
from job_scout.discovery.providers.remotive import RemotiveProvider
from job_scout.discovery.queries import profile_queries
from job_scout.discovery.registration import register_discovered
from job_scout.discovery.registry import Registry
from job_scout.discovery.settings import load_settings, select_country
from job_scout.discovery.validate import Validation
from job_scout.matching.profile import load_profile
from job_scout.matching.scoring import rank_jobs
from job_scout.models import utc_now
from job_scout.notifications.digest import build_digest, job_key
from job_scout.sources.base import BoardHTTP
from job_scout.storage.database import open_database
from job_scout.storage.insights import Insights
from job_scout.storage.repository import Repository


async def fetch_discovery(configuration):
    settings = configuration.providers.arbeitnow
    if not settings.enabled:
        return None, 0
    async with public_http.public_api_client(allowed_hosts={"www.arbeitnow.com"}) as client:
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
    async with public_http.public_api_client(allowed_hosts={"himalayas.app"}) as client:
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


async def fetch_brave(configuration, profile, engine, *, dry_run=False):
    settings = configuration.providers.brave
    if not settings.enabled:
        return None, 0
    now = utc_now()
    if dry_run:
        return PublisherDiscoveryResult(
            attempted_at=now, completed_at=now, status="dry_run_skipped"
        ), 0
    token = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    error = None
    if not token or not token.isascii() or any(ord(c) < 33 for c in token):
        error = "Brave enabled but BRAVE_SEARCH_API_KEY is missing or invalid"
    elif engine is None:
        error = "Brave requires persistent request budgeting in an initialized database"
    if error:
        return PublisherDiscoveryResult(
            attempted_at=now, completed_at=now, status="unavailable", error=error
        ), 0
    budget = SearchBudget(engine, settings.daily_request_limit, settings.monthly_request_limit)
    registry = Registry(engine)
    known = registry.sources()
    known_companies = {(r["provider"], r["region"], r["board_id"]): r["company"] for r in known}
    blocked = {
        (row["provider"], row["region"], row["board_id"])
        for row in known
        if row["status"] == "disabled"
    }
    for row in registry.candidates():
        if row["state"] == "rejected":
            candidate = detect(row["url"])
            if candidate.config:
                c = candidate.config
                blocked.add((c.source, c.region, c.board_id))
    # Separate clients: the search credential can NEVER reach a publisher endpoint.
    async with (
        httpx.AsyncClient(
            transport=public_http.PublicAPITransport(allowed_hosts={"api.search.brave.com"}),
            follow_redirects=False,
            trust_env=False,
            headers={"X-Subscription-Token": token, "Accept": "application/json"},
        ) as search_client,
        public_http.public_api_client() as publisher_client,
    ):
        search_http = BraveHTTP(search_client, settings, budget.reserve)
        publisher_http = BoardHTTP(
            publisher_client,
            max_requests=settings.publisher_max_requests,
            max_bytes=settings.max_response_bytes,
            interval=settings.interval_seconds,
            attempts=settings.attempts,
        )
        result = await discover_publishers(
            search_http,
            publisher_http,
            settings,
            profile,
            blocked_boards=blocked,
            known_companies=known_companies,
        )
        return result, result.requests


async def fetch_remotive(configuration, engine, *, dry_run=False):
    settings = configuration.providers.remotive
    if not settings.enabled:
        return None, 0
    now = utc_now()
    if engine is None:
        return DiscoveryResult(
            provider="remotive",
            attempted_at=now,
            completed_at=now,
            error="Remotive requires persistent refresh scheduling",
        ), 0
    if dry_run or not Insights(engine).claim_refresh("remotive", settings.refresh_hours, now=now):
        # Read-only previews never spend this provider's daily allowance. Reuse recent
        # observations without advancing freshness or marking the provider healthy.
        jobs = [
            DiscoveredJob.model_validate(j.model_dump())
            for j in Repository(engine).jobs()
            if j.source == "remotive"
            and j.fetched_at >= now - timedelta(hours=settings.refresh_hours)
        ]
        return DiscoveryResult(
            provider="remotive",
            attempted_at=now,
            completed_at=now,
            jobs=jobs,
            status="dry_run_skipped" if dry_run else "cached",
        ), 0
    async with public_http.public_api_client(allowed_hosts={"remotive.com"}) as client:
        http = BoardHTTP(
            client,
            attempts=1,
            max_requests=1,
            max_bytes=settings.max_response_bytes,
            interval=settings.interval_seconds,
        )
        result = await discover(RemotiveProvider(http, settings.limit), settings)
        result.requests = http.requests
        result.limited = len(result.jobs) + result.skipped >= settings.limit
        return result, result.requests


async def fetch_providers(configuration, profile, *, engine=None, dry_run=False):
    responses = await asyncio.gather(
        fetch_discovery(configuration),
        fetch_himalayas(configuration, profile),
        fetch_remotive(configuration, engine, dry_run=dry_run),
        fetch_brave(configuration, profile, engine, dry_run=dry_run),
    )
    results = [result for result, _ in responses if result is not None]
    return (
        DiscoveryBatch(results=results) if results else None,
        sum(requests for _, requests in responses),
    )


async def register_result(result, engine, configuration, *, dry_run):
    async with public_http.public_api_client() as client:
        registry = Registry(engine)
        settings = configuration.registration
        outcomes, deferred, registered = [], 0, 0
        results = result.results if isinstance(result, DiscoveryBatch) else [result]
        known = {(r["provider"], r["region"], r["board_id"]): r for r in registry.sources()}
        if settings.enabled:
            for discovery in results:
                if not isinstance(discovery, PublisherDiscoveryResult):
                    continue
                for source in discovery.sources:
                    if not source.jobs:
                        continue
                    candidate = detect(str(source.jobs[0].source_url), name=source.config.company)
                    c = source.config
                    existing = known.get((c.source, c.region, c.board_id))
                    if existing and existing["status"] in {"active", "disabled"}:
                        continue
                    if registered >= settings.max_new_boards_per_run:
                        deferred += 1
                        continue
                    verdict = Validation(
                        "verified",
                        "Independently fetched publisher posting identity verified",
                        True,
                        len(source.jobs),
                    )
                    state = "verified"
                    if not dry_run:
                        state = registry.record(
                            candidate,
                            verdict,
                            method="publisher_discovery",
                            evidence={
                                "publisher_posting_ids": [j.source_job_id for j in source.jobs]
                            },
                        )["state"]
                    outcomes.append(
                        {
                            "board": candidate.url,
                            "state": state,
                            "reason": verdict.reason,
                            "validation": "publisher_verified",
                        }
                    )
                    registered += 1
        report = await register_discovered(
            result,
            registry,
            client,
            settings.model_copy(
                update={"max_new_boards_per_run": settings.max_new_boards_per_run - registered}
            ),
            dry_run=dry_run,
        )
        report.outcomes = outcomes + report.outcomes
        report.deferred += deferred
        return report


def add_command(subcommands) -> None:
    command = subcommands.add_parser(
        "discover-jobs",
        help="Find jobs across unknown employers using free feeds and optional metered search",
    )
    command.add_argument("--config", type=Path, help="Discovery YAML; bounded defaults if omitted")
    command.add_argument("--profile", type=Path, default=Path("config/profile.yaml"))
    command.add_argument(
        "--country", help="Country name for enabled search providers; e.g. Hong Kong"
    )
    command.add_argument("--track", help="Optional career track in the matching profile")
    command.add_argument("--dry-run", action="store_true", help="Fetch and rank without saving")
    command.add_argument("--format", choices=["text", "json"], default="text")
    command.add_argument(
        "--limit", type=int, help="Maximum matches shown; defaults to profile limit"
    )


def execute(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        configuration = select_country(load_settings(args.config), args.country)
        profile = load_profile(args.profile).for_track(args.track)
        if args.limit is not None:
            if not 1 <= args.limit <= 1000:
                raise ValueError("Limit must be between 1 and 1000")
            profile.digest.maximum_matches = args.limit
    except (OSError, ValueError, yaml.YAMLError) as exc:
        parser.exit(2, f"Cannot load discovery settings or profile ({type(exc).__name__}).\n")
    if not any(
        p.enabled
        for p in (
            configuration.providers.arbeitnow,
            configuration.providers.himalayas,
            configuration.providers.brave,
            configuration.providers.remotive,
        )
    ):
        report = {"status": "disabled"}
        print(json.dumps(report) if args.format == "json" else render_discovery(report))
        return 0
    engine = open_database()
    try:
        repository = Repository(engine)

        batch, requests = asyncio.run(
            fetch_providers(configuration, profile, engine=engine, dry_run=args.dry_run)
        )
        assert batch is not None
        counts = None
        provider_counts = {}
        if not args.dry_run:
            for result in batch.results:
                ingested = repository.ingest_discovery(result)
                Insights(engine).record(result, ingested, profile)
                current = asdict(ingested)
                provider_counts[result.provider] = current
                counts = {key: (counts or {}).get(key, 0) + value for key, value in current.items()}

        registration = asyncio.run(
            register_result(batch, engine, configuration, dry_run=args.dry_run)
        )
        # Rank only this run's observations; preserve first-seen dates for shared sorting.
        history = repository.jobs(include_closed=True)
        reconciled = reconcile_jobs(batch.jobs, history, repository.job_links())
        if not args.dry_run:
            repository.save_job_links(reconciled.new_links)
            reconciled = reconcile_jobs(batch.jobs, history, repository.job_links())
        jobs = reconciled.jobs
        visible = Insights(engine).filter_jobs(jobs, profile.feedback_scope, reconciled.aliases)
        matches = [
            m for m in rank_jobs(visible, profile) if m.score >= profile.digest.minimum_score
        ]
        failures = []
        for result in batch.results:
            if result.error:
                failures.append(f"{result.provider}: {result.error}")
            if result.skipped and not isinstance(result, PublisherDiscoveryResult):
                failures.append(f"{result.provider}: skipped {result.skipped} invalid postings")
        previews = build_digest(
            matches,
            now=max(result.completed_at for result in batch.results),
            timezone=profile.digest.timezone,
            failures=failures,
            successful_sources=sum(result.pages > 0 for result in batch.results),
            maximum_matches=profile.digest.maximum_matches,
            discovery_preview=True,
            cached_sources=sum(
                getattr(r, "status", "completed") in {"cached", "dry_run_skipped"} and bool(r.jobs)
                for r in batch.results
            ),
        )
        output = {
            "provider": ", ".join(result.provider for result in batch.results),
            "pages": sum(result.pages for result in batch.results),
            "requests": requests,
            "fetched_jobs": sum(
                len(r.jobs)
                for r in batch.results
                if getattr(r, "status", "completed") == "completed"
            ),
            "cached_jobs": sum(
                len(r.jobs)
                for r in batch.results
                if getattr(r, "status", "completed") in {"cached", "dry_run_skipped"}
            ),
            "unique_jobs": len(jobs),
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
                "matching_jobs": sum(
                    bool(set(reconciled.aliases[job_key(m.job)]) & {job_key(j) for j in r.jobs})
                    for m in matches
                ),
                "status": getattr(r, "status", "completed"),
                "publisher_requests": getattr(r, "publisher_requests", 0),
                "failed_boards": getattr(r, "failed_boards", 0),
                "deferred": getattr(r, "deferred", 0),
                "ingestion": provider_counts.get(r.provider),
            }
            for r in batch.results
        ]
        output["matches"] = [
            {"job_key": job_key(match.job)}
            | match.model_dump(
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
