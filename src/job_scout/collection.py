"""Bounded collection with independent employer outcomes."""

import asyncio
import logging

import httpx
from pydantic import ValidationError

from job_scout.models import CollectionReport, SourceConfig, SourceRegistry, SourceResult, utc_now
from job_scout.sources import get_source
from job_scout.sources.base import BoardHTTP, SourceError

logger = logging.getLogger(__name__)


async def collect(
    registry: SourceRegistry,
    *,
    client: httpx.AsyncClient,
    concurrency: int = 3,
    interval: float = 0.5,
) -> CollectionReport:
    if concurrency < 1:
        raise ValueError("concurrency must be positive")
    http = BoardHTTP(client, interval=interval)
    semaphore = asyncio.Semaphore(concurrency)

    async def fetch(config: SourceConfig) -> SourceResult:
        async with semaphore:
            attempted = utc_now()
            try:
                jobs = await get_source(config.source, http).fetch_jobs(config)
            except (SourceError, ValidationError) as exc:
                reason = (
                    str(exc) if isinstance(exc, SourceError) else "Malformed canonical job data"
                )
                logger.warning(
                    "source_failed source=%s board=%s reason=%s",
                    config.source,
                    config.board_id,
                    reason,
                )
                return SourceResult(
                    config=config, attempted_at=attempted, completed_at=utc_now(), error=reason
                )
            logger.info(
                "source_collected source=%s board=%s jobs=%d",
                config.source,
                config.board_id,
                len(jobs),
            )
            return SourceResult(
                config=config, attempted_at=attempted, completed_at=utc_now(), jobs=jobs
            )

    results = await asyncio.gather(
        *(fetch(config) for config in registry.sources if config.enabled)
    )
    return CollectionReport(sources=list(results))
