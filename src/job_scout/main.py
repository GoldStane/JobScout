"""CLI orchestration; parsing and persistence live behind module boundaries."""

import argparse
import asyncio
import json
import logging
import os
from collections.abc import Sequence
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path

import httpx
import yaml
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from job_scout.collection import collect
from job_scout.config import load_registry
from job_scout.matching.profile import Profile, load_profile
from job_scout.matching.scoring import rank_jobs
from job_scout.models import CollectionReport, SourceRegistry, utc_now
from job_scout.notifications.service import run_digest
from job_scout.notifications.telegram import DeliveryError, TelegramSender
from job_scout.storage.database import (
    SCHEMA_VERSION,
    SchemaError,
    initialize_database,
    migrate_database,
    open_database,
)
from job_scout.storage.delivery import DeliveryBusy, DeliveryRepository, recipient_key
from job_scout.storage.repository import Repository
from job_scout.validation import validate_config


async def _collect(registry: SourceRegistry) -> CollectionReport:
    async with httpx.AsyncClient(
        follow_redirects=False,
        headers={"User-Agent": "JobScout/0.1", "Accept": "application/json"},
    ) as client:
        return await collect(registry, client=client)


def _registry(path: Path, parser: argparse.ArgumentParser) -> SourceRegistry:
    try:
        registry = load_registry(path)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        parser.exit(
            2, f"Cannot load source registry ({type(exc).__name__}); check path and schema.\n"
        )
    if not any(source.enabled for source in registry.sources):
        logging.getLogger(__name__).warning(
            "No enabled sources; replace placeholders and enable boards"
        )
    return registry


def _database_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    profile = None
    if args.command in {"match", "run"}:
        try:
            profile = load_profile(args.profile).for_track(args.track)
            data = profile.model_dump()
            if args.sort:
                data["sort"] = [
                    dict(zip(("field", "direction"), term.split(":"), strict=True))
                    for term in args.sort.split(",")
                ]
            for name in [
                "employment_types",
                "allowed_locations",
                "excluded_locations",
                "remote_types",
            ]:
                override = getattr(args, name)
                if override is not None:
                    data["filters"][name] = override
            for name in ["unknown_employment_type", "unknown_location", "unknown_remote_type"]:
                override = getattr(args, name)
                if override is not None:
                    data["filters"][name] = override
            profile = Profile.model_validate(data)
        except (OSError, yaml.YAMLError, ValueError) as exc:
            parser.exit(
                2,
                f"Cannot load matching profile or track ({type(exc).__name__}); "
                "check the profile schema and track name.\n",
            )
    engine = open_database()
    try:
        if args.command in {"db-init", "db-migrate"}:
            (initialize_database if args.command == "db-init" else migrate_database)(engine)
            print(json.dumps({"schema_version": SCHEMA_VERSION, "initialized": True}))
            return 0
        repository = Repository(engine)
        if args.command == "delivery-status":
            chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
            if not chat_id:
                parser.exit(2, "Set TELEGRAM_CHAT_ID to inspect delivery history.\n")
            print(json.dumps(DeliveryRepository(engine).status(recipient_key(chat_id)), indent=2))
        elif args.command == "run":
            assert profile is not None
            if args.limit is not None:
                profile.digest.maximum_matches = args.limit
            if args.min_score is not None:
                profile.digest.minimum_score = args.min_score
            registry = _registry(args.config, parser)

            async def execute():
                chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or None
                token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
                async with httpx.AsyncClient() as client:
                    sender = None
                    if not args.dry_run:
                        try:
                            sender = TelegramSender(client, token, chat_id or "")
                        except ValueError as exc:
                            parser.exit(2, f"{exc}\n")
                    elif chat_id is None:
                        logging.getLogger(__name__).warning(
                            "No TELEGRAM_CHAT_ID: preview treats all matching jobs as undelivered"
                        )
                    return await run_digest(
                        repository=repository,
                        deliveries=DeliveryRepository(engine),
                        profile=profile,
                        collect=lambda: _collect(registry),
                        chat_id=chat_id,
                        sender=sender,
                        now=utc_now(),
                        dry_run=args.dry_run,
                        retry_uncertain=args.retry_uncertain,
                    )

            result = asyncio.run(execute())
            for index, message in enumerate(result.messages, start=1):
                print(f"--- Preview message {index} ---\n{message}\n")
            print(json.dumps({"status": result.status, "source_failures": result.source_failures}))
            return 1 if result.source_failures else 0
        elif args.command == "match":
            assert profile is not None
            jobs = repository.jobs()
            ranked = rank_jobs(jobs, profile, include_ineligible=True)
            rejected = sum(item.eligibility.status == "ineligible" for item in ranked)
            selected = [
                item
                for item in ranked
                if item.score >= args.min_score
                and (args.include_ineligible or item.eligibility.status != "ineligible")
            ]
            limit = args.limit if args.limit is not None else profile.digest.maximum_matches
            print(
                json.dumps(
                    {
                        "total_open_jobs": len(jobs),
                        "filtered_out_jobs": len(jobs) - len(ranked),
                        "ineligible_jobs": rejected,
                        "matching_jobs_before_limit": len(selected),
                        "matches": [
                            item.model_dump(
                                mode="json",
                                exclude={
                                    "job": {
                                        "description_text",
                                        "source_metadata",
                                        "eligibility_information",
                                    }
                                },
                            )
                            for item in selected[:limit]
                        ],
                    },
                    indent=2,
                )
            )
        elif args.command == "jobs":
            print(
                json.dumps(
                    [
                        job.model_dump(mode="json")
                        for job in repository.jobs(include_closed=args.include_closed)
                    ],
                    indent=2,
                )
            )
        elif args.command == "source-health":
            print(
                json.dumps(
                    repository.source_health(), default=lambda date: date.isoformat(), indent=2
                )
            )
        else:
            report = asyncio.run(_collect(_registry(args.config, parser)))
            counts = repository.ingest(report)
            print(json.dumps(asdict(counts), indent=2))
            return 1 if report.failed else 0
        return 0
    finally:
        engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="job-scout",
        description="JobScout: personal job discovery and explainable matching.",
        epilog="Collection, persistence, and matching with daily Telegram digests.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('job-scout')}")
    subcommands = parser.add_subparsers(dest="command")
    for name, help_text in [
        ("collect", "Fetch public boards and print JSON without saving"),
        ("ingest", "Fetch public boards and persist jobs and source health"),
    ]:
        command = subcommands.add_parser(name, help=help_text)
        command.add_argument("--config", type=Path, default=Path("config/companies.yaml"))
    subcommands.add_parser("db-init", help="Initialize the database selected by DATABASE_URL")
    subcommands.add_parser("db-migrate", help="Migrate an existing database to the current schema")
    subcommands.add_parser("delivery-status", help="Inspect Telegram delivery outcomes")
    subcommands.add_parser("telegram-chats", help="Read private chat IDs from recent bot updates")
    run = subcommands.add_parser("run", help="Collect, match, and send or preview the daily digest")
    run.add_argument("--config", type=Path, default=Path("config/companies.yaml"))
    run.add_argument("--profile", type=Path, default=Path("config/profile.yaml"))
    run.add_argument("--track")
    run.add_argument("--limit", type=int)
    run.add_argument("--min-score", type=float)
    run.add_argument(
        "--dry-run", action="store_true", help="Preview without writes or Telegram sends"
    )
    run.add_argument(
        "--retry-uncertain",
        action="store_true",
        help="Retry ambiguous sends; duplicates are possible",
    )
    jobs = subcommands.add_parser("jobs", help="Print stored jobs as JSON")
    jobs.add_argument("--include-closed", action="store_true")
    subcommands.add_parser("source-health", help="Print persistent collection health as JSON")
    matching = subcommands.add_parser("match", help="Rank stored open jobs using a private profile")
    matching.add_argument("--profile", type=Path, default=Path("config/profile.yaml"))
    matching.add_argument("--track", help="Optional career_tracks entry in the profile")
    matching.add_argument(
        "--limit", type=int, help="Maximum results; defaults to profile digest limit"
    )
    matching.add_argument("--min-score", type=float, default=0.0)
    matching.add_argument(
        "--include-ineligible", action="store_true", help="Include exclusions for review"
    )
    validation = subcommands.add_parser(
        "validate-config", help="Validate YAML offline without database or network access"
    )
    validation.add_argument("--profile", type=Path, default=Path("config/profile.yaml"))
    validation.add_argument("--config", type=Path, default=Path("config/companies.yaml"))
    for command in [run, matching]:
        command.add_argument(
            "--sort", help="Priority order, e.g. score:desc,posted_at:desc,location:asc"
        )
        command.add_argument(
            "--employment-types", nargs="+", choices=["internship", "graduate", "entry_level"]
        )
        command.add_argument("--allowed-locations", nargs="+")
        command.add_argument("--excluded-locations", nargs="+")
        command.add_argument("--remote-types", nargs="+", choices=["remote", "hybrid", "on_site"])
        for name in ["employment-type", "location", "remote-type"]:
            command.add_argument(f"--unknown-{name}", choices=["include", "exclude"])
    args = parser.parse_args(argv)
    if args.command == "validate-config":
        return validate_config(args.profile, args.config)
    if args.command in {"match", "run"} and args.sort:
        terms = args.sort.split(",")
        if any(len(term.split(":")) != 2 or not all(term.split(":")) for term in terms):
            parser.error("--sort requires field:asc or field:desc separated by commas")
    if args.command in {"match", "run"}:
        if args.limit is not None and not 1 <= args.limit <= 1000:
            parser.error("--limit must be between 1 and 1000")
        if args.min_score is not None and not 0 <= args.min_score <= 100:
            parser.error("--min-score must be between 0 and 100")
    if args.command is None:
        parser.print_help()
        return 0
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if args.command == "collect":
        report = asyncio.run(_collect(_registry(args.config, parser)))
        print(report.model_dump_json(indent=2))
        return 1 if report.failed else 0
    try:
        if args.command == "telegram-chats":

            async def discover():
                async with httpx.AsyncClient() as client:
                    try:
                        sender = TelegramSender(
                            client, os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), "0"
                        )
                    except ValueError as exc:
                        parser.exit(2, f"{exc}\n")
                    return await sender.private_chats()

            print(json.dumps({"private_chat_ids": asyncio.run(discover())}))
            return 0
        return _database_command(args, parser)
    except (DeliveryError, DeliveryBusy) as exc:
        parser.exit(4, f"{exc}\n")
    except SchemaError as exc:
        parser.exit(3, f"{exc}\n")
    except (SQLAlchemyError, OSError, ValueError, ImportError) as exc:
        # Driver exceptions may contain credentials and SQL values. Do not echo them.
        parser.exit(
            3,
            f"Database operation failed ({type(exc).__name__}); check DATABASE_URL, "
            "database availability and installed driver.\n",
        )
