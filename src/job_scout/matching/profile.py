"""Private matching profile validation and career-track overrides."""

from datetime import date
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from job_scout.models import NonEmpty


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Education(Settings):
    degree: Literal["high_school", "associate", "bachelors", "masters", "doctorate"] | None = None
    field: NonEmpty | None = None
    expected_graduation_date: date | None = None
    expected_graduation_year: Annotated[int, Field(strict=True, ge=1900, le=9999)] | None = None
    currently_enrolled: StrictBool | None = None

    @field_validator("expected_graduation_date", mode="before")
    @classmethod
    def explicit_date(cls, value):
        if isinstance(value, (int, float)):
            raise ValueError("Use YYYY-MM-DD or expected_graduation_year")
        return value

    @model_validator(mode="after")
    def one_graduation_precision(self):
        if self.expected_graduation_date is not None and self.expected_graduation_year is not None:
            raise ValueError("Set graduation date OR year, not both")
        return self


class EligibilitySettings(Settings):
    work_authorized_countries: list[NonEmpty] | None = None
    requires_sponsorship: StrictBool | None = None
    willing_to_relocate: StrictBool | None = None


class Preferences(Settings):
    search_worldwide: Literal[True] = True
    preferred_regions: list[NonEmpty] = Field(default_factory=list)
    employment_types: list[Literal["internship", "graduate", "entry_level"]] = Field(
        default_factory=list
    )
    primary_roles: list[NonEmpty] = Field(default_factory=list)
    secondary_roles: list[NonEmpty] = Field(default_factory=list)
    # Explicit user terms extend the small built-in Europe vocabulary.
    location_terms: dict[str, list[NonEmpty]] = Field(default_factory=dict)


Weight = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]


class RankingWeights(Settings):
    role_alignment: Weight = 0.35
    skills_alignment: Weight = 0.25
    education_experience_alignment: Weight = 0.20
    compensation_career_potential: Weight = 0.10
    location_preference: Weight = 0.10

    @model_validator(mode="after")
    def positive_total(self) -> "RankingWeights":
        if sum(self.model_dump().values()) <= 0:
            raise ValueError("At least one ranking weight must be positive")
        return self


class DigestSettings(Settings):
    send_after: Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")] = "08:00"
    minimum_score: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] = 60.0
    timezone: str = "Europe/Rome"
    maximum_matches: Annotated[int, Field(strict=True, ge=1, le=1000)] = 10

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Use a valid IANA timezone") from exc
        return value


class CareerTrack(Settings):
    primary_roles: list[NonEmpty]
    secondary_roles: list[NonEmpty] = Field(default_factory=list)
    skills: list[NonEmpty] | None = None
    ranking_weights: RankingWeights | None = None


class Filters(Settings):
    employment_types: list[Literal["internship", "graduate", "entry_level"]] = Field(
        default_factory=list
    )
    allowed_locations: list[NonEmpty] = Field(default_factory=list)
    excluded_locations: list[NonEmpty] = Field(default_factory=list)
    remote_types: list[Literal["remote", "hybrid", "on_site"]] = Field(default_factory=list)
    unknown_employment_type: Literal["include", "exclude"] = "include"
    unknown_location: Literal["include", "exclude"] = "include"
    unknown_remote_type: Literal["include", "exclude"] = "include"


class SortRule(Settings):
    field: Literal["score", "posted_at", "first_seen_at", "location", "company", "title"]
    direction: Literal["asc", "desc"] = "desc"


class Profile(Settings):
    feedback_scope: str = Field(default="default", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    filters: Filters = Field(default_factory=Filters)
    sort: Annotated[list[SortRule], Field(min_length=1)] = Field(
        default_factory=lambda: [SortRule(field="score", direction="desc")]
    )

    @field_validator("sort")
    @classmethod
    def unique_sort_fields(cls, rules):
        if len({rule.field for rule in rules}) != len(rules):
            raise ValueError("Each sort field may appear only once")
        return rules

    education: Education = Field(default_factory=Education)
    eligibility: EligibilitySettings = Field(default_factory=EligibilitySettings)
    preferences: Preferences = Field(default_factory=Preferences)
    skills: list[NonEmpty] = Field(default_factory=list)
    ranking_weights: RankingWeights = Field(default_factory=RankingWeights)
    digest: DigestSettings = Field(default_factory=DigestSettings)
    career_tracks: dict[str, CareerTrack] = Field(default_factory=dict)

    def for_track(self, name: str | None) -> "Profile":
        if name is None:
            return self
        if name not in self.career_tracks:
            raise ValueError("Unknown career track")
        track = self.career_tracks[name]
        return self.model_copy(
            update={
                "preferences": self.preferences.model_copy(
                    update={
                        "primary_roles": track.primary_roles,
                        "secondary_roles": track.secondary_roles,
                    }
                ),
                "skills": track.skills if track.skills is not None else self.skills,
                "ranking_weights": track.ranking_weights or self.ranking_weights,
            }
        )


def load_profile(path: Path) -> Profile:
    with path.open(encoding="utf-8") as stream:
        return Profile.model_validate(yaml.safe_load(stream))
