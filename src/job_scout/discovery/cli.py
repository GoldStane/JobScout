"""Inspect and administer persistent employer and ATS registrations."""

import asyncio
import json
from pathlib import Path

from job_scout.discovery import http as public_http
from job_scout.discovery.detect import DiscoveryError, detect
from job_scout.discovery.registry import Registry
from job_scout.discovery.validate import Validation, validate
from job_scout.storage.database import open_database


def add_commands(subcommands):
    company = subcommands.add_parser(
        "company", aliases=["companies"], help="Manage persistent company/source registrations"
    )
    commands = company.add_subparsers(dest="company_command", required=True)
    seed = commands.add_parser(
        "import-seeds", help="Validate optional YAML seeds without collecting jobs or notifying"
    )
    seed.add_argument("--config", type=Path, default=Path("config/companies.yaml"))
    seed.add_argument("--dry-run", action="store_true", help="Validate without database writes")
    seed.add_argument("--max-sources", type=int, default=100)
    seed.add_argument("--max-requests", type=int, default=60)
    seed.add_argument("--max-bytes", type=int, default=2_000_000)
    seed.add_argument("--max-runtime-seconds", type=int, default=120)
    for name in ["add", "revalidate"]:
        command = commands.add_parser(name)
        if name == "add":
            command.add_argument("url", help="Public Greenhouse, Lever, or Ashby careers URL")
            command.add_argument(
                "--name", help="User-supplied display label (not verified legal identity)"
            )
            command.add_argument(
                "--company-id",
                type=int,
                help="Explicitly link another board to an existing company",
            )
        else:
            command.add_argument("candidate_id", type=int)
        command.add_argument(
            "--dry-run", action="store_true", help="Validate over HTTP without database writes"
        )
        command.add_argument("--max-requests", type=int, default=12)
        command.add_argument("--max-bytes", type=int, default=2_000_000)
    commands.add_parser("list", help="List persistent registrations and collection health")
    commands.add_parser("candidates", help="List validation/review results and provenance")
    for name in ["approve", "reject"]:
        command = commands.add_parser(name)
        command.add_argument("candidate_id", type=int)
    for name in ["disable", "enable"]:
        command = commands.add_parser(
            name, help="Change source monitoring without deleting history"
        )
        command.add_argument("source_id", type=int)


def execute(args) -> int:
    if args.company_command == "import-seeds":
        from job_scout.discovery.seeds import execute as import_seeds

        return import_seeds(args)
    if args.company_command in {"add", "revalidate"}:
        if not 1 <= args.max_requests <= 100 or not 1024 <= args.max_bytes <= 10_000_000:
            raise DiscoveryError("Use max-requests 1..100 and max-bytes 1024..10000000")
    candidate = detect(args.url, name=args.name) if args.company_command == "add" else None
    # A standalone URL preview needs neither a database nor initialization.
    if candidate and args.dry_run:
        registry = None
        engine = None
    else:
        engine = open_database()
        registry = Registry(engine)
    try:
        command = args.company_command
        if command in {"add", "revalidate"}:
            if command == "revalidate":
                candidate = registry.candidate(args.candidate_id)
            if not args.dry_run:
                registry.record(candidate, Validation("pending_validation", "Validation requested"))

            async def verify():
                async with public_http.public_api_client() as client:
                    return await validate(
                        candidate, client, max_requests=args.max_requests, max_bytes=args.max_bytes
                    )

            result = asyncio.run(verify())
            if args.dry_run:
                output = {
                    "url": candidate.url,
                    "state": result.state,
                    "reason": result.reason,
                    "endpoint_valid": result.endpoint_valid,
                    "jobs_found": result.jobs_found,
                    "dry_run": True,
                }
            else:
                output = registry.record(
                    candidate, result, company_id=getattr(args, "company_id", None)
                )
            print(json.dumps(output, default=str, indent=2))
            return 1 if result.state == "pending_validation" else 0
        if command == "list":
            output = registry.sources()
        elif command == "candidates":
            output = registry.candidates()
        else:
            getattr(registry, command)(
                args.source_id if command in {"disable", "enable"} else args.candidate_id
            )
            output = {"status": command}
        print(json.dumps(output, default=str, indent=2))
        return 0
    finally:
        if engine is not None:
            engine.dispose()
