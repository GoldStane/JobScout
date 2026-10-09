"""Lever public Postings API adapter, including EU boards and pagination."""

from pydantic import BaseModel, Field, HttpUrl, TypeAdapter, ValidationError

from job_scout.models import Job, NonEmpty, SourceConfig, utc_now
from job_scout.normalize import html_to_text
from job_scout.sources.base import BoardHTTP, SourceError


class _Categories(BaseModel):
    location: str | None = None
    commitment: str | None = None
    team: str | None = None
    department: str | None = None


class _Section(BaseModel):
    text: str = ""
    content: str = ""


class _Posting(BaseModel):
    id: NonEmpty
    text: NonEmpty
    hostedUrl: HttpUrl
    applyUrl: HttpUrl
    categories: _Categories = Field(default_factory=_Categories)
    description: str | None = None
    descriptionPlain: str | None = None
    additional: str | None = None
    additionalPlain: str | None = None
    lists: list[_Section] = Field(default_factory=list)
    workplaceType: str | None = None


class LeverSource:
    def __init__(self, http: BoardHTTP, *, page_size: int = 100, max_pages: int = 100) -> None:
        if page_size < 1 or max_pages < 1:
            raise ValueError("Pagination limits must be positive")
        self.http = http
        self.page_size = page_size
        self.max_pages = max_pages

    async def fetch_jobs(self, config: SourceConfig) -> list[Job]:
        host = "api.eu.lever.co" if config.region == "eu" else "api.lever.co"
        posts: list[_Posting] = []
        seen: set[str] = set()
        for page in range(self.max_pages):
            payload = await self.http.get_json(
                f"https://{host}/v0/postings/{config.board_id}",
                {"mode": "json", "skip": page * self.page_size, "limit": self.page_size},
            )
            try:
                batch = TypeAdapter(list[_Posting]).validate_python(payload)
            except ValidationError as exc:
                raise SourceError(
                    "Invalid Lever response; required fields or types are malformed"
                ) from exc
            for post in batch:
                if post.id in seen:
                    raise SourceError("Lever pagination repeated a posting; retry collection later")
                seen.add(post.id)
            posts.extend(batch)
            if len(batch) < self.page_size:
                break
        else:
            raise SourceError("Lever pagination limit reached; collection is incomplete")
        now = utc_now()
        jobs = []
        for post in posts:
            sections = [post.descriptionPlain or html_to_text(post.description or "")]
            sections.extend(
                f"{html_to_text(section.text)}\n{html_to_text(section.content)}"
                for section in post.lists
            )
            sections.append(post.additionalPlain or html_to_text(post.additional or ""))
            jobs.append(
                Job(
                    source="lever",
                    source_job_id=post.id,
                    source_region=config.region,
                    company=config.company,
                    company_identifier=config.board_id,
                    title=post.text,
                    location_text=post.categories.location,
                    employment_type=post.categories.commitment,
                    remote_type={"remote": "remote", "hybrid": "hybrid", "on-site": "on_site"}.get(
                        post.workplaceType
                    ),
                    description_text="\n".join(
                        section.strip() for section in sections if section.strip()
                    ),
                    application_url=post.applyUrl,
                    source_url=post.hostedUrl,
                    fetched_at=now,
                    first_seen_at=now,
                    last_seen_at=now,
                    source_metadata={
                        "team": post.categories.team,
                        "department": post.categories.department,
                    },
                )
            )
        return jobs
