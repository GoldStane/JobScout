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
    currently_enrolled: StrictBool | None = None


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


class Profile(Settings):
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
