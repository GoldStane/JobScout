"""Employer-independent discovery; deliberately separate from ATS snapshots."""

from typing import Protocol

from pydantic import BaseModel, Field

from job_scout.models import FeedProvider, Job, UTCDateTime


class DiscoveredJob(Job):
    source: FeedProvider = "arbeitnow"
    # This is a provider identity namespace, never an inferred employer board.
    company_identifier: str = "feed"
    application_url: None = None


class DiscoveryPage(BaseModel):
    jobs: list[DiscoveredJob] = Field(default_factory=list)
    next_cursor: str | None = None
    skipped: int = 0
    expired: int = 0


class DiscoveryProvider(Protocol):
    name: str

    async def discover_jobs(
        self, query: str | None = None, cursor: str | None = None
    ) -> DiscoveryPage: ...


class DiscoveryResult(BaseModel):
    provider: FeedProvider
    attempted_at: UTCDateTime
    completed_at: UTCDateTime
    jobs: list[DiscoveredJob] = Field(default_factory=list)
    pages: int = 0
    skipped: int = 0
    limited: bool = False
    error: str | None = None
    expired: int = 0
    requests: int = 0
    queries: int = 0


class DiscoveryBatch(BaseModel):
    results: list[DiscoveryResult] = Field(default_factory=list)

    @property
    def jobs(self) -> list[DiscoveredJob]:
        return [job for result in self.results for job in result.jobs]
