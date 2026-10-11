"""Bounded feed collection; preserve earlier pages when a later request fails."""

import asyncio

from job_scout.discovery.base import DiscoveryProvider, DiscoveryResult
from job_scout.discovery.settings import ArbeitnowSettings
from job_scout.models import utc_now
from job_scout.sources.base import SourceError


async def discover(
    provider: DiscoveryProvider,
    settings: ArbeitnowSettings,
    *,
    queries: list[str | None] | None = None,
) -> DiscoveryResult:
    result = DiscoveryResult(provider=provider.name, attempted_at=utc_now(), completed_at=utc_now())
    seen: set[str] = set()
    try:
        async with asyncio.timeout(settings.max_runtime_seconds):
            for query in queries or [None]:
                result.queries += 1
                cursor = None
                query_seen: set[str] = set()
                cursors: set[str] = set()
                for _ in range(settings.max_pages):
                    page = await provider.discover_jobs(query=query, cursor=cursor)
                    result.pages += 1
                    result.skipped += page.skipped
                    result.expired += page.expired
                    ids = {job.source_job_id for job in page.jobs}
                    for job in page.jobs:
                        if job.source_job_id not in seen:
                            seen.add(job.source_job_id)
                            result.jobs.append(job)
                    if page.next_cursor is None:
                        break
                    if page.next_cursor in cursors or (ids and not ids - query_seen):
                        raise SourceError("Repeated feed page; pagination stopped")
                    if not page.jobs and not (page.expired or page.skipped):
                        break
                    query_seen.update(ids)
                    cursors.add(page.next_cursor)
                    cursor = page.next_cursor
                else:
                    result.limited = True
    except SourceError as exc:
        result.error = str(exc)
    except TimeoutError:
        result.error = "Discovery runtime budget exhausted"
    result.completed_at = utc_now()
    return result
