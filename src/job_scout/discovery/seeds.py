"""Explicit, bounded YAML seed validation into the existing persistent registry."""

import asyncio
import json
from collections import Counter

import yaml

from job_scout.config import load_registry
from job_scout.discovery import http as public_http
from job_scout.discovery.detect import DiscoveryError, detect
from job_scout.discovery.registry import Registry, board_url
from job_scout.discovery.validate import validate
from job_scout.models import SourceRegistry
from job_scout.sources.base import BoardHTTP
from job_scout.storage.database import open_database


async def import_seeds(
    seeds,
    registry,
    client,
    *,
    dry_run=False,
    max_requests=60,
    max_bytes=2_000_000,
    max_runtime_seconds=120,
):
    existing = {
        (row["provider"], row["region"], row["board_id"]): row
        for row in registry.sources()
        if row["status"] != "unregistered"
    }
    rejected = {row["url"] for row in registry.candidates() if row["state"] == "rejected"}
    http = BoardHTTP(client, max_requests=max_requests, max_bytes=max_bytes)
    results = []
    deadline = asyncio.get_running_loop().time() + max_runtime_seconds
    for config in seeds.sources:
        key = (config.source, config.region, config.board_id)
        candidate = detect(board_url(config), name=config.company)
        output = {"provider": config.source, "region": config.region, "board_id": config.board_id}
        if key in existing:
            output.update(state="preserved", status=existing[key]["status"])
        elif candidate.url in rejected:
            output.update(state="rejected")
        elif not config.enabled:
            if not dry_run:
                registry.seed(SourceRegistry(sources=[config]))
            output.update(state="disabled")
        elif asyncio.get_running_loop().time() >= deadline or http.requests >= max_requests:
            output.update(state="deferred")
        else:
            try:
                async with asyncio.timeout_at(deadline):
                    validation = await validate(candidate, client, http=http)
            except TimeoutError:
                output.update(state="deferred")
            else:
                if dry_run:
                    output.update(state=validation.state, reason=validation.reason)
                else:
                    saved = registry.record(candidate, validation, method="manual_seed")
                    output.update(state=saved["state"], reason=saved["reason"])
        results.append(output)
    return {
        "dry_run": dry_run,
        "requests": http.requests,
        "counts": dict(Counter(row["state"] for row in results)),
        "results": results,
    }


def execute(args):
    if not (
        1 <= args.max_sources <= 1000
        and 1 <= args.max_requests <= 1000
        and 1024 <= args.max_bytes <= 10_000_000
        and 1 <= args.max_runtime_seconds <= 600
    ):
        raise DiscoveryError("Invalid seed import limits; see company import-seeds --help")
    try:
        seeds = load_registry(args.config) if args.config.exists() else SourceRegistry(sources=[])
    except (OSError, ValueError, yaml.YAMLError):
        raise DiscoveryError("Cannot load seed YAML; check path and schema") from None
    if len(seeds.sources) > args.max_sources:
        raise DiscoveryError("Seed file exceeds --max-sources; split it or increase the limit")
    engine = open_database()
    try:
        registry = Registry(engine)

        async def run():
            async with public_http.public_api_client() as client:
                return await import_seeds(
                    seeds,
                    registry,
                    client,
                    dry_run=args.dry_run,
                    max_requests=args.max_requests,
                    max_bytes=args.max_bytes,
                    max_runtime_seconds=args.max_runtime_seconds,
                )

        report = asyncio.run(run())
        print(json.dumps(report, indent=2))
        return int(
            any(
                report["counts"].get(state)
                for state in ("pending_validation", "needs_review", "rejected", "deferred")
            )
        )
    finally:
        engine.dispose()
