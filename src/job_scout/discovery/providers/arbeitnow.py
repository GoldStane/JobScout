"""Arbeitnow's no-key public feed. Listing URLs are not application URLs."""

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, Field, HttpUrl, StrictBool, StrictInt, ValidationError

from job_scout.discovery.base import DiscoveredJob, DiscoveryPage
from job_scout.discovery.registration import ats_links
from job_scout.models import NonEmpty, utc_now
from job_scout.normalize import html_to_text
from job_scout.sources.base import BoardHTTP, SourceError

ENDPOINT = "https://www.arbeitnow.com/api/job-board-api"
# Regional listing hosts observed in the official feed; these are never fetched.
LISTING_HOSTS = {"www.arbeitnow.com", "www.arbeitnow.co.uk", "www.arbeitnow.ch", "www.arbeitnow.fr"}


class _Posting(BaseModel):
    slug: NonEmpty
    company_name: NonEmpty
    title: NonEmpty
    url: HttpUrl
    description: str = ""
    location: str | None = None
    remote: StrictBool | None = None
    created_at: StrictInt | None = None
    tags: list[str] = Field(default_factory=list)
    job_types: list[str] = Field(default_factory=list)


class ArbeitnowProvider:
    name = "arbeitnow"

    def __init__(self, http: BoardHTTP):
        self.http = http

    async def discover_jobs(
        self, query: str | None = None, cursor: str | None = None
    ) -> DiscoveryPage:
        if query is not None:
            raise SourceError("Arbeitnow feed has no keyword search; use the shared matcher")
        if cursor is not None and (
            len(cursor) > 5 or not cursor.isascii() or not cursor.isdecimal()
        ):
            raise SourceError("Invalid Arbeitnow page cursor")
        page = int(cursor or "1")
        if not 1 <= page <= 10000:
            raise SourceError("Invalid Arbeitnow page cursor")
        payload = await self.http.get_json(ENDPOINT, {"page": page})
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise SourceError("Arbeitnow response requires a data array")
        links = payload.get("links")
        if not isinstance(links, dict) or "next" not in links:
            raise SourceError("Arbeitnow response requires links.next")
        next_cursor = self._next_cursor(links["next"], page)
        fetched = utc_now()
        jobs, skipped = [], 0
        for raw in payload["data"]:
            try:
                item = _Posting.model_validate(raw)
                url = item.url
                if (
                    url.scheme != "https"
                    or url.host not in LISTING_HOSTS
                    or url.username is not None
                    or url.password is not None
                    or url.port != 443
                    or not (url.path or "").startswith("/jobs/")
                ):
                    raise ValueError("Unsafe or unsupported provider listing URL")
                posted = (
                    datetime.fromtimestamp(item.created_at, UTC)
                    if item.created_at is not None
                    else None
                )
                jobs.append(
                    DiscoveredJob(
                        source_job_id=item.slug,
                        company=item.company_name,
                        title=item.title,
                        description_text=html_to_text(item.description, encoded=True),
                        location_text=item.location,
                        # False does not distinguish hybrid from on-site.
                        remote_type="remote" if item.remote is True else None,
                        employment_type=", ".join(item.job_types) or None,
                        source_url=url,
                        listing_url=url,
                        attribution="Arbeitnow",
                        attribution_url="https://www.arbeitnow.com/",
                        posted_at=posted,
                        fetched_at=fetched,
                        first_seen_at=fetched,
                        last_seen_at=fetched,
                        source_metadata={
                            "tags": item.tags,
                            "remote": item.remote,
                            "ats_board_leads": ats_links(item.description),
                            "ats_posting_leads": ats_links(item.description, postings=True),
                        },
                    )
                )
            except (ValidationError, ValueError, OverflowError, OSError):
                # One malformed item must not discard other employers. Report lost coverage.
                skipped += 1
        return DiscoveryPage(jobs=jobs, next_cursor=next_cursor, skipped=skipped)

    @staticmethod
    def _next_cursor(value: object, page: int) -> str | None:
        if value is None:
            return None
        try:
            if not isinstance(value, str):
                raise ValueError
            url = urlsplit(value)
            params = parse_qs(url.query, strict_parsing=True)
            if (
                url.scheme != "https"
                or url.netloc != "www.arbeitnow.com"
                or url.path != "/api/job-board-api"
                or url.fragment
                or set(params) != {"page"}
                or len(params["page"]) != 1
                or params["page"][0] != str(page + 1)
            ):
                raise ValueError
            # Never follow response URLs: reconstruct a request to the fixed endpoint.
            return str(page + 1)
        except ValueError:
            raise SourceError("Invalid Arbeitnow pagination link") from None
