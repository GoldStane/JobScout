"""Employer-independent discovery; deliberately separate from ATS snapshots."""

from typing import Literal, Protocol

from pydantic import BaseModel, Field

from job_scout.models import FeedProvider, Job, SourceResult, UTCDateTime


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
    status: Literal["completed", "cached", "dry_run_skipped"] = "completed"
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


class PublisherDiscoveryResult(BaseModel):
    """Only independently retrieved publisher jobs may cross this retention boundary.

    No search URLs, snippets, result objects or query-dependent fields belong here.
    """

    provider: Literal["brave"] = "brave"
    attempted_at: UTCDateTime
    completed_at: UTCDateTime
    sources: list[SourceResult] = Field(default_factory=list)
    pages: int = 0
    requests: int = 0
    queries: int = 0
    skipped: int = 0
    expired: int = 0
    limited: bool = False
    error: str | None = None
    status: Literal["completed", "dry_run_skipped", "unavailable"] = "completed"
    publisher_requests: int = 0
    failed_boards: int = 0
    deferred: int = 0
    leads_seen: int = 0

    @property
    def jobs(self) -> list[Job]:
        return [job for result in self.sources for job in result.jobs]


class DiscoveryBatch(BaseModel):
    results: list[DiscoveryResult | PublisherDiscoveryResult] = Field(default_factory=list)

    @property
    def jobs(self) -> list[Job]:
        return [job for result in self.results for job in result.jobs]
