"""Bounded collection with independent employer outcomes."""

import asyncio
import logging

import httpx

from job_scout.models import CollectionReport, SourceConfig, SourceRegistry, SourceResult, utc_now
from job_scout.sources.ashby import AshbySource
from job_scout.sources.base import BoardHTTP, JobSource, SourceError
from job_scout.sources.greenhouse import GreenhouseSource
from job_scout.sources.lever import LeverSource

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
    adapters: dict[str, JobSource] = {
        "ashby": AshbySource(http),
        "greenhouse": GreenhouseSource(http),
        "lever": LeverSource(http),
    }
    semaphore = asyncio.Semaphore(concurrency)

    async def fetch(config: SourceConfig) -> SourceResult:
        async with semaphore:
            attempted = utc_now()
            try:
                jobs = await adapters[config.source].fetch_jobs(config)
            except SourceError as exc:
                logger.warning(
                    "source_failed source=%s board=%s reason=%s",
                    config.source,
                    config.board_id,
                    exc,
                )
                return SourceResult(
                    config=config, attempted_at=attempted, completed_at=utc_now(), error=str(exc)
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
