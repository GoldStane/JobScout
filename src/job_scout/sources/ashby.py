"""Ashby public Job Board API adapter."""

from datetime import datetime, UTC
from typing import Any

from pydantic import BaseModel, HttpUrl, TypeAdapter, ValidationError

from job_scout.models import Job, NonEmpty, SourceConfig, utc_now
from job_scout.normalize import html_to_text
from job_scout.sources.base import BoardHTTP, SourceError


class _Address(BaseModel):
    addressLocality: str | None = None
    addressRegion: str | None = None
    addressCountry: str | None = None


class _Compensation(BaseModel):
    compensationTierSummary: str | None = None
    scrapeableCompensationSalarySummary: str | None = None


class _Posting(BaseModel):
    id: NonEmpty
    title: NonEmpty
    location: str | None = None
    secondaryLocations: list[dict[str, Any]] = []
    department: str | None = None
    team: str | None = None
    isRemote: bool | None = None
    workplaceType: str | None = None
    descriptionHtml: str | None = None
    descriptionPlain: str | None = None
    publishedAt: str | None = None
    employmentType: str | None = None
    address: _Address | None = None
    jobUrl: HttpUrl
    applyUrl: HttpUrl
    isListed: bool = True
    compensation: _Compensation | None = None


class _Response(BaseModel):
    jobs: list[_Posting]


class AshbySource:
    def __init__(self, http: BoardHTTP) -> None:
        self.http = http

    async def fetch_jobs(self, config: SourceConfig) -> list[Job]:
        payload = await self.http.get_json(
            f"https://api.ashbyhq.com/posting-api/job-board/{config.board_id}",
            {"includeCompensation": "true"},
        )
        try:
            response = _Response.model_validate(payload)
        except ValidationError as exc:
            raise SourceError(
                "Invalid Ashby response; required fields or types are malformed"
            ) from exc

        now = utc_now()
        jobs = []
        for post in response.jobs:
            if not post.isListed:
                continue

            # Build location text from address or location field
            location_parts = []
            if post.address:
                if post.address.addressLocality:
                    location_parts.append(post.address.addressLocality)
                if post.address.addressRegion:
                    location_parts.append(post.address.addressRegion)
                if post.address.addressCountry:
                    location_parts.append(post.address.addressCountry)
            location_text = ", ".join(location_parts) if location_parts else post.location

            # Map workplace type
            remote_type: str | None = None
            if post.workplaceType:
                remote_type = {
                    "Remote": "remote",
                    "Hybrid": "hybrid",
                    "OnSite": "on_site",
                }.get(post.workplaceType)

            # Parse published date
            posted_at: datetime | None = None
            if post.publishedAt:
                try:
                    posted_at = datetime.fromisoformat(post.publishedAt.replace("Z", "+00:00"))
                except ValueError:
                    pass

            # Build description
            description = post.descriptionPlain or html_to_text(post.descriptionHtml or "", encoded=True)

            # Compensation summary
            compensation_summary = None
            if post.compensation:
                compensation_summary = post.compensation.compensationTierSummary or post.compensation.scrapeableCompensationSalarySummary

            jobs.append(
                Job(
                    source="ashby",
                    source_job_id=post.id,
                    company=config.company,
                    company_identifier=config.board_id,
                    title=post.title,
                    location_text=location_text,
                    remote_type=remote_type,
                    description_text=description,
                    employment_type=post.employmentType,
                    application_url=post.applyUrl,
                    source_url=post.jobUrl,
                    posted_at=posted_at,
                    fetched_at=now,
                    first_seen_at=now,
                    last_seen_at=now,
                    source_metadata={
                        "department": post.department,
                        "team": post.team,
                        "is_remote": post.isRemote,
                        "compensation_summary": compensation_summary,
                        "secondary_locations": post.secondaryLocations,
                    },
                )
            )
        return jobs