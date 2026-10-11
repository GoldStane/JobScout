"""Remotive public feed: one bounded request, listing attribution, no direct URL claims."""

from datetime import datetime

from pydantic import BaseModel, HttpUrl, StrictInt, ValidationError

from job_scout.discovery.base import DiscoveredJob, DiscoveryPage
from job_scout.discovery.registration import ats_links
from job_scout.models import NonEmpty, utc_now
from job_scout.normalize import html_to_text
from job_scout.sources.base import SourceError


class _Posting(BaseModel):
    id: StrictInt
    url: HttpUrl
    title: NonEmpty
    company_name: NonEmpty
    description: str = ""
    publication_date: str | None = None
    candidate_required_location: str | None = None
    job_type: str | None = None
    salary: str | None = None


class RemotiveProvider:
    name = "remotive"

    def __init__(self, http, limit=100):
        self.http, self.limit = http, limit

    async def discover_jobs(self, query=None, cursor=None):
        if query is not None or cursor is not None:
            raise SourceError("Remotive uses one bounded feed request per refresh")
        payload = await self.http.get_json(
            "https://remotive.com/api/remote-jobs", {"limit": self.limit}
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise SourceError("Remotive response requires a jobs array")
        if len(payload["jobs"]) > self.limit:
            raise SourceError("Remotive response exceeds configured job limit")
        fetched = utc_now()
        jobs, skipped = [], 0
        for raw in payload["jobs"]:
            try:
                item = _Posting.model_validate(raw)
                url = item.url
                if (
                    item.id <= 0
                    or url.scheme != "https"
                    or url.host != "remotive.com"
                    or url.port != 443
                    or url.username is not None
                    or url.password is not None
                    or not (url.path or "").startswith("/remote-jobs/")
                ):
                    raise ValueError("Invalid Remotive identity or listing URL")
                posted = None
                if item.publication_date:
                    parsed = datetime.fromisoformat(item.publication_date)
                    # Official examples omit timezone: retain the value, never invent UTC.
                    if parsed.tzinfo is not None:
                        posted = parsed
                jobs.append(
                    DiscoveredJob(
                        source="remotive",
                        source_job_id=str(item.id),
                        company=item.company_name,
                        title=item.title,
                        description_text=html_to_text(item.description, encoded=True),
                        location_text=item.candidate_required_location or None,
                        remote_type="remote",
                        employment_type=item.job_type or None,
                        salary_text=item.salary or None,
                        source_url=url,
                        listing_url=url,
                        attribution="Remotive",
                        attribution_url=url,
                        posted_at=posted,
                        fetched_at=fetched,
                        first_seen_at=fetched,
                        last_seen_at=fetched,
                        source_metadata={
                            "publication_date": item.publication_date,
                            "ats_board_leads": ats_links(item.description),
                            "ats_posting_leads": ats_links(item.description, postings=True),
                        },
                    )
                )
            except (ValidationError, ValueError):
                skipped += 1
        return DiscoveryPage(jobs=jobs, skipped=skipped)
