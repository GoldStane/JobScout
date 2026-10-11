"""Bounded, resumable text seed import using the existing discovery pipeline."""

import asyncio
import hashlib
import json
from collections import Counter
from pathlib import Path

import httpx

from job_scout.discovery.detect import SUPPORTED, DiscoveryError, detect
from job_scout.discovery.http import PublicAPITransport
from job_scout.discovery.registry import Registry
from job_scout.discovery.validate import Validation, validate
from job_scout.sources.base import BoardHTTP
from job_scout.storage.database import open_database

MAX_INPUT_BYTES = 1_000_000


def add_command(subcommands):
    command = subcommands.add_parser(
        "discover-sources", help="Validate and register careers URLs from a text file"
    )
    command.add_argument(
        "--input",
        required=True,
        type=Path,
        help="UTF-8 file: one careers URL per line; # comments allowed",
    )
    command.add_argument(
        "--dry-run", action="store_true", help="Validate without opening/writing a database"
    )
    command.add_argument(
        "--max-urls", type=int, default=100, help="Maximum non-comment input lines (default 100)"
    )
    command.add_argument(
        "--max-requests-per-provider",
        type=int,
        default=60,
        help="Shared request budget including retries (default 60)",
    )
    command.add_argument(
        "--max-bytes", type=int, default=2_000_000, help="Maximum bytes per API response"
    )


def read_input(path: Path, maximum: int) -> tuple[list[tuple[int, str]], str]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise DiscoveryError("Input exceeds 1 MB; split it into smaller files")
        content = raw.decode("utf-8-sig")
    except (OSError, UnicodeError):
        raise DiscoveryError("Cannot read input; use an accessible UTF-8 text file") from None
    entries = [
        (line, value.strip())
        for line, value in enumerate(content.splitlines(), 1)
        if value.strip() and not value.lstrip().startswith("#")
    ]
    if not entries:
        raise DiscoveryError("Input contains no URLs")
    if len(entries) > maximum:
        raise DiscoveryError("Input exceeds --max-urls; split the file or increase the limit")
    return entries, hashlib.sha256(raw).hexdigest()


async def discover(
    entries, fingerprint, client, *, registry=None, max_requests=60, max_bytes=2_000_000
):
    # Sequential processing avoids concurrent registry writes and limits load. Each
    # provider shares pacing and its total request budget across all input boards.
    transports = {
        provider: BoardHTTP(client, max_requests=max_requests, max_bytes=max_bytes)
        for provider in SUPPORTED
    }
    seen = {}
    rejected = (
        {row["url"] for row in registry.candidates() if row["state"] == "rejected"}
        if registry
        else set()
    )
    results = []
    for line, url in entries:
        try:
            candidate = detect(url)
        except DiscoveryError as exc:
            # Never echo credential-bearing/invalid input URLs into output.
            results.append({"line": line, "state": "invalid", "reason": str(exc)})
            continue
        if candidate.identity in seen:
            results.append(
                {
                    "line": line,
                    "url": candidate.url,
                    "state": "duplicate",
                    "duplicate_of_line": seen[candidate.identity],
                }
            )
            continue
        seen[candidate.identity] = line
        if candidate.url in rejected:
            results.append(
                {
                    "line": line,
                    "url": candidate.url,
                    "state": "rejected",
                    "reason": "Existing operator rejection preserved; no request made",
                }
            )
            continue
        evidence = {"input_sha256": fingerprint, "line": line}
        if registry:
            registry.record(
                candidate,
                Validation("pending_validation", "Text import validation requested"),
                method="text_import",
                evidence=evidence,
            )
        provider = candidate.config.source if candidate.config else None
        result = await validate(candidate, client, http=transports.get(provider))
        if registry:
            stored = registry.record(candidate, result, method="text_import", evidence=evidence)
            output = {
                key: stored[key]
                for key in ["id", "url", "state", "reason", "source_id", "endpoint_valid"]
            }
        else:
            output = {
                "url": candidate.url,
                "state": result.state,
                "reason": result.reason,
                "endpoint_valid": result.endpoint_valid,
            }
        results.append({"line": line, **output, "jobs_found": result.jobs_found})
    return {
        "dry_run": registry is None,
        "input_sha256": fingerprint,
        "counts": dict(Counter(row["state"] for row in results)),
        "results": results,
    }


def execute(args) -> int:
    if (
        not 1 <= args.max_urls <= 1000
        or not 1 <= args.max_requests_per_provider <= 1000
        or not 1024 <= args.max_bytes <= 10_000_000
    ):
        raise DiscoveryError(
            "Use max-urls and max-requests-per-provider 1..1000; max-bytes 1024..10000000"
        )
    entries, fingerprint = read_input(args.input, args.max_urls)
    engine = None
    try:
        registry = None
        if not args.dry_run:
            engine = open_database()
            registry = Registry(engine)

        async def run():
            async with httpx.AsyncClient(
                transport=PublicAPITransport(),
                trust_env=False,
                follow_redirects=False,
                headers={"User-Agent": "JobScout/0.1", "Accept": "application/json"},
            ) as client:
                return await discover(
                    entries,
                    fingerprint,
                    client,
                    registry=registry,
                    max_requests=args.max_requests_per_provider,
                    max_bytes=args.max_bytes,
                )

        result = asyncio.run(run())
        print(json.dumps(result, indent=2))
        return (
            1
            if any(result["counts"].get(state, 0) for state in ["invalid", "pending_validation"])
            else 0
        )
    finally:
        if engine is not None:
            engine.dispose()
