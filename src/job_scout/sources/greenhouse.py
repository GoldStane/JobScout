"""Greenhouse public Job Board API adapter."""

from pydantic import BaseModel, HttpUrl, StrictInt, ValidationError

from job_scout.models import Job, NonEmpty, SourceConfig, UTCDateTime, utc_now
from job_scout.normalize import html_to_text
from job_scout.sources.base import BoardHTTP, SourceError


class _Location(BaseModel):
    name: str | None = None


class _Posting(BaseModel):
    id: StrictInt
    title: NonEmpty
    absolute_url: HttpUrl
    location: _Location | None = None
    content: str | None = None
    updated_at: UTCDateTime | None = None
    first_published: UTCDateTime | None = None
    internal_job_id: StrictInt | None = None
    requisition_id: str | None = None


class _Response(BaseModel):
    jobs: list[_Posting]


class GreenhouseSource:
    def __init__(self, http: BoardHTTP) -> None:
        self.http = http

    async def fetch_jobs(self, config: SourceConfig) -> list[Job]:
        payload = await self.http.get_json(
            f"https://boards-api.greenhouse.io/v1/boards/{config.board_id}/jobs",
            {"content": "true"},
        )
        try:
            response = _Response.model_validate(payload)
        except ValidationError as exc:
            raise SourceError(
                "Invalid Greenhouse response; required fields or types are malformed"
            ) from exc
        now = utc_now()
        return [
            Job(
                source="greenhouse",
                source_job_id=str(post.id),
                company=config.company,
                company_identifier=config.board_id,
                title=post.title,
                location_text=post.location.name if post.location else None,
                description_text=html_to_text(post.content or "", encoded=True),
                application_url=post.absolute_url,
                source_url=post.absolute_url,
                posted_at=post.first_published,
                updated_at=post.updated_at,
                fetched_at=now,
                first_seen_at=now,
                last_seen_at=now,
                source_metadata={
                    "internal_job_id": post.internal_job_id,
                    "requisition_id": post.requisition_id,
                },
            )
            for post in response.jobs
        ]
