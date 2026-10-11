"""Free remote-jobs API: opaque browse cursors or profile-driven paged search."""

import hashlib
from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, Field, HttpUrl, StrictFloat, StrictInt, ValidationError

from job_scout.discovery.base import DiscoveredJob, DiscoveryPage
from job_scout.discovery.registration import ats_links
from job_scout.discovery.settings import HimalayasSettings
from job_scout.models import NonEmpty, utc_now
from job_scout.normalize import html_to_text
from job_scout.sources.base import BoardHTTP, SourceError

ENDPOINT = "https://himalayas.app/jobs/api"
Number = Annotated[StrictInt | StrictFloat, Field(allow_inf_nan=False)]
Salary = Annotated[Number, Field(ge=0)]


class _Country(BaseModel):
    name: NonEmpty


class _Posting(BaseModel):
    guid: NonEmpty
    title: NonEmpty
    companyName: NonEmpty
    applicationLink: HttpUrl
    description: str | None = None
    excerpt: str | None = None
    companySlug: str | None = None
    employmentType: str | None = None
    locationRestrictions: list[NonEmpty | _Country] | None = None
    timezoneRestrictions: list[str | Number] | None = None
    seniority: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    minSalary: Salary | None = None
    maxSalary: Salary | None = None
    salaryPeriod: str | None = None
    currency: str | None = None
    pubDate: str | Number | None = None
    expiryDate: str | Number | None = None


def timestamp(value: str | int | float | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("Naive timestamp")
        return result.astimezone(UTC)
    # Live responses use Unix seconds; the documented example also permits milliseconds.
    return datetime.fromtimestamp(value / 1000 if value >= 100_000_000_000 else value, UTC)


class HimalayasProvider:
    name = "himalayas"

    def __init__(self, http: BoardHTTP, settings: HimalayasSettings):
        self.http, self.settings = http, settings

    async def discover_jobs(
        self, query: str | None = None, cursor: str | None = None
    ) -> DiscoveryPage:
        if self.settings.mode == "browse":
            if query is not None:
                raise SourceError("Keyword queries require Himalayas search mode")
            params: dict[str, str | int] = {"limit": self.settings.page_size}
            if cursor is not None:
                params["cursor"] = cursor  # Opaque; never decode, parse or construct.
            endpoint = ENDPOINT
        else:
            if cursor is not None and (
                len(cursor) > 5 or not cursor.isascii() or not cursor.isdecimal()
            ):
                raise SourceError("Invalid Himalayas search page")
            page = int(cursor or "1")
            if not 1 <= page <= 10000:
                raise SourceError("Invalid Himalayas search page")
            params = {"page": page, "sort": self.settings.sort}
            if query:
                params["q"] = query
            if self.settings.country:
                params["country"] = self.settings.country
            if self.settings.worldwide_only:
                params["worldwide"] = "true"
            endpoint = ENDPOINT + "/search"
        payload = await self.http.get_json(endpoint, params)
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise SourceError("Himalayas response requires a jobs array")
        if self.settings.mode == "browse":
            next_cursor = payload.get("nextCursor")
            if next_cursor is not None and (
                not isinstance(next_cursor, str) or not next_cursor or len(next_cursor) > 8192
            ):
                raise SourceError("Invalid Himalayas cursor")
        else:
            total, limit = payload.get("totalCount"), payload.get("limit")
            if (
                type(total) is not int
                or total < 0
                or type(limit) is not int
                or not 1 <= limit <= 20
            ):
                raise SourceError("Invalid Himalayas search pagination metadata")
            next_cursor = str(page + 1) if payload["jobs"] and page * limit < total else None
        now = utc_now()
        jobs, skipped, expired = [], 0, 0
        for raw in payload["jobs"]:
            try:
                item = _Posting.model_validate(raw)
                url = item.applicationLink
                if (
                    url.scheme != "https"
                    or url.host != "himalayas.app"
                    or url.port != 443
                    or url.username is not None
                    or url.password is not None
                    or not (url.path or "").startswith("/companies/")
                    or "/jobs/" not in (url.path or "")
                ):
                    raise ValueError("Unsafe or unsupported listing URL")
                posted, expiry = timestamp(item.pubDate), timestamp(item.expiryDate)
                if expiry is not None and expiry <= now:
                    expired += 1
                    continue
                locations = (
                    None
                    if item.locationRestrictions is None
                    else [
                        entry if isinstance(entry, str) else entry.name
                        for entry in item.locationRestrictions
                    ]
                )
                zones = (
                    None
                    if item.timezoneRestrictions is None
                    else [str(entry) for entry in item.timezoneRestrictions]
                )
                salary = None
                if item.minSalary is not None or item.maxSalary is not None:
                    if (
                        item.minSalary is not None
                        and item.maxSalary is not None
                        and item.minSalary > item.maxSalary
                    ):
                        raise ValueError("Reversed salary range")
                    low = str(item.minSalary) if item.minSalary is not None else "unknown"
                    high = str(item.maxSalary) if item.maxSalary is not None else "unknown"
                    salary = f"{low}–{high} {item.currency or '(currency unknown)'} / " + (
                        item.salaryPeriod or "period unknown"
                    )
                description = item.description or item.excerpt or ""
                jobs.append(
                    DiscoveredJob(
                        source="himalayas",
                        source_job_id=hashlib.sha256(item.guid.encode()).hexdigest(),
                        company=item.companyName,
                        title=item.title,
                        description_text=html_to_text(description, encoded=True),
                        employment_type=item.employmentType,
                        remote_type="remote",
                        location_text=", ".join(locations)
                        if locations
                        else (
                            "Worldwide (remote)"
                            if locations == []
                            else "Remote (restrictions unknown)"
                        ),
                        timezone_restrictions=zones,
                        salary_text=salary,
                        source_url=url,
                        listing_url=url,
                        attribution="Himalayas",
                        attribution_url="https://himalayas.app/",
                        posted_at=posted,
                        fetched_at=now,
                        first_seen_at=now,
                        last_seen_at=now,
                        source_metadata={
                            "guid": item.guid,
                            "company_slug": item.companySlug,
                            "location_restrictions": locations,
                            "timezone_restrictions": zones,
                            "salary": {
                                "min": item.minSalary,
                                "max": item.maxSalary,
                                "currency": item.currency,
                                "period": item.salaryPeriod,
                            },
                            "seniority": item.seniority,
                            "categories": item.categories,
                            "expires_at": expiry.isoformat() if expiry else None,
                            "ats_board_leads": ats_links(description),
                            "ats_posting_leads": ats_links(description, postings=True),
                        },
                    )
                )
            except (ValidationError, ValueError, OverflowError, OSError):
                skipped += 1
        return DiscoveryPage(jobs=jobs, next_cursor=next_cursor, skipped=skipped, expired=expired)
