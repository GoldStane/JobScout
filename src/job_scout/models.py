"""Validated configuration and canonical collection records."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    model_validator,
)

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
UTCDateTime = Annotated[AwareDatetime, AfterValidator(lambda value: value.astimezone(UTC))]
Provider = Literal["greenhouse", "lever", "ashby", "smartrecruiters"]
FeedProvider = Literal["arbeitnow", "himalayas"]
FEED_PROVIDERS = {"arbeitnow", "himalayas"}


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Provider
    company: NonEmpty
    board_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]+$", max_length=200)]
    enabled: bool = False
    region: Literal["global", "eu"] = "global"

    @model_validator(mode="after")
    def validate_source(self) -> "SourceConfig":
        if self.source in {"greenhouse", "ashby"} and self.region != "global":
            raise ValueError("region=eu is only supported for Lever")
        if self.enabled and self.board_id.startswith("REPLACE_WITH_"):
            raise ValueError("Replace placeholder board IDs before enabling a source")
        return self


class SourceRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sources: list[SourceConfig]

    @model_validator(mode="after")
    def unique_sources(self) -> "SourceRegistry":
        keys = [(s.source, s.region, s.board_id) for s in self.sources]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate source/region/board_id entries")
        return self


class Job(BaseModel):
    """A published posting; unknown facts remain null rather than inferred."""

    model_config = ConfigDict(extra="forbid")
    source: Provider | FeedProvider
    source_job_id: NonEmpty
    company: NonEmpty
    company_identifier: NonEmpty
    source_region: Literal["global", "eu"] = "global"
    title: NonEmpty
    location_text: str | None = None
    remote_type: Literal["remote", "hybrid", "on_site"] | None = None
    description_text: str = ""
    employment_type: str | None = None
    application_url: HttpUrl | None = None
    source_url: HttpUrl
    listing_url: HttpUrl | None = None
    attribution: str | None = None
    attribution_url: HttpUrl | None = None
    attribution_links: dict[str, HttpUrl] = Field(default_factory=dict)
    salary_text: str | None = None
    timezone_restrictions: list[str] | None = None
    posted_at: UTCDateTime | None = None
    fetched_at: UTCDateTime
    first_seen_at: UTCDateTime
    last_seen_at: UTCDateTime
    updated_at: UTCDateTime | None = None
    status: Literal["open", "closed"] = "open"
    eligibility_information: dict[str, str] = Field(default_factory=dict)
    source_metadata: dict[str, object] = Field(default_factory=dict)


class SourceResult(BaseModel):
    config: SourceConfig
    attempted_at: UTCDateTime
    completed_at: UTCDateTime
    jobs: list[Job] = Field(default_factory=list)
    error: str | None = None


class CollectionReport(BaseModel):
    sources: list[SourceResult]

    @property
    def failed(self) -> bool:
        return any(source.error is not None for source in self.sources)


def utc_now() -> datetime:
    return datetime.now(UTC)
