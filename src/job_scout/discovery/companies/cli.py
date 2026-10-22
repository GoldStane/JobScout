"""Opt-in company search command; never sends notifications or writes seed YAML."""

import asyncio
import json
import os
from pathlib import Path

import httpx
import yaml

from job_scout.discovery import http as public_http
from job_scout.discovery.budgets import SearchBudget
from job_scout.discovery.companies.careers import SiteTransport
from job_scout.discovery.companies.search import CompanyReport, company_queries, discover_companies
from job_scout.discovery.detect import DiscoveryError
from job_scout.discovery.providers.brave import BraveHTTP
from job_scout.discovery.registry import Registry
from job_scout.discovery.settings import load_settings
from job_scout.matching.profile import load_profile
from job_scout.models import utc_now
from job_scout.sources.base import BoardHTTP
from job_scout.storage.database import open_database
from job_scout.storage.insights import Insights


def add_command(subcommands):
    command = subcommands.add_parser(
        "discover-companies", help="Search employer interests and register verified ATS boards"
    )
    command.add_argument(
        "--config", type=Path, help="Opt-in discovery settings (required for a new run)"
    )
    command.add_argument("--profile", type=Path, default=Path("config/profile.yaml"))
    command.add_argument("--track")
    command.add_argument(
        "--last-report",
        action="store_true",
        help="Read saved diagnostic counts and next due time; no network search",
    )
    command.add_argument(
        "--retry-now",
        action="store_true",
        help="Explicitly bypass company cooldown; metered request quotas still apply",
    )
    command.add_argument(
        "--dry-run", action="store_true", help="Preview queries; no HTTP or writes"
    )


async def fetch(config, profile, engine, *, retry_now=False):
    settings = config.providers.brave
    token = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    if not token or not token.isascii() or any(ord(c) < 33 for c in token):
        raise DiscoveryError("Company discovery requires a valid BRAVE_SEARCH_API_KEY")
    insights = Insights(engine)
    if not insights.claim_refresh(
        "company_search", config.companies.refresh_hours, force=retry_now
    ):
        return CompanyReport(status="not_due", next_due=insights.next_refresh("company_search"))
    budget = SearchBudget(engine, settings.daily_request_limit, settings.monthly_request_limit)
    async with (
        httpx.AsyncClient(
            transport=public_http.PublicAPITransport(allowed_hosts={"api.search.brave.com"}),
            follow_redirects=False,
            trust_env=False,
            headers={"X-Subscription-Token": token, "Accept": "application/json"},
        ) as search_client,
        httpx.AsyncClient(
            transport=SiteTransport(),
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "JobScout/0.1", "Accept": "text/html,text/plain"},
        ) as site_client,
        public_http.public_api_client() as publisher_client,
    ):
        kwargs = dict(
            attempts=settings.attempts,
            interval=settings.interval_seconds,
            max_bytes=settings.max_response_bytes,
        )
        result = await discover_companies(
            BraveHTTP(search_client, settings, budget.reserve),
            BoardHTTP(site_client, max_requests=config.companies.max_site_requests, **kwargs),
            BoardHTTP(publisher_client, max_requests=settings.publisher_max_requests, **kwargs),
            Registry(engine),
            profile,
            config,
        )
        result.next_due = insights.next_refresh("company_search")
        return result


def execute(args):
    if args.last_report:
        if args.retry_now or args.dry_run:
            raise DiscoveryError("--last-report cannot be combined with --retry-now or --dry-run")
        engine = open_database()
        try:
            print(json.dumps(Insights(engine).last_company_report(), indent=2))
            return 0
        finally:
            engine.dispose()
    if args.config is None:
        raise DiscoveryError("Provide --config for discovery, or use --last-report")
    try:
        config = load_settings(args.config)
        profile = load_profile(args.profile).for_track(args.track)
    except (OSError, ValueError, yaml.YAMLError):
        raise DiscoveryError(
            "Cannot load company discovery settings or profile; check YAML"
        ) from None
    if args.dry_run:
        print(
            json.dumps(
                {
                    "status": "dry_run",
                    "enabled": config.companies.enabled,
                    "queries": company_queries(
                        profile,
                        config.companies,
                        config.providers.brave.max_queries,
                        utc_now().date(),
                        config.providers.brave.country,
                    ),
                },
                indent=2,
            )
        )
        return 0
    if not config.companies.enabled:
        raise DiscoveryError("Set companies.enabled: true to opt in to metered company search")
    engine = open_database()
    try:
        started = utc_now()
        result = asyncio.run(fetch(config, profile, engine, retry_now=args.retry_now))
        if result.status != "not_due":
            Insights(engine).record_companies(result, started)
        print(json.dumps(result.as_dict(), indent=2))
        return 1 if result.status in {"partial", "failed"} else 0
    finally:
        engine.dispose()
