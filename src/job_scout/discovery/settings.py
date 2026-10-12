"""Validated bounded feed settings; job interests stay in the shared profile."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ArbeitnowSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = True
    max_pages: int = Field(default=3, ge=1, le=20)
    max_requests: int = Field(default=9, ge=1, le=60)
    attempts: int = Field(default=3, ge=1, le=3)
    interval_seconds: float = Field(default=1.0, ge=0.5, le=60)
    max_runtime_seconds: float = Field(default=120.0, gt=0, le=600)
    max_response_bytes: int = Field(default=5_000_000, ge=1024, le=10_000_000)


class HimalayasSettings(ArbeitnowSettings):
    enabled: bool = False
    mode: Literal["browse", "search"] = "browse"
    page_size: int = Field(default=20, ge=1, le=20)
    max_queries: int = Field(default=3, ge=1, le=10)
    country: str | None = Field(default=None, min_length=1, max_length=100)
    worldwide_only: bool = False
    sort: Literal["recent", "relevant"] = "recent"

    @model_validator(mode="after")
    def search_filters(self):
        if self.mode == "browse" and (self.country or self.worldwide_only or self.sort != "recent"):
            raise ValueError("Himalayas country, worldwide_only and sort require search mode")
        if self.country and self.worldwide_only:
            raise ValueError("Choose country or worldwide_only, not both")
        if self.country is not None:
            self.country = self.country.strip()
            if not self.country:
                raise ValueError("Country cannot be blank")
        return self


class BraveSettings(ArbeitnowSettings):
    enabled: bool = False
    country: str | None = Field(default=None, min_length=1, max_length=80)
    max_queries: int = Field(default=3, ge=1, le=10)
    max_pages: int = Field(default=1, ge=1, le=10)
    max_requests: int = Field(default=3, ge=1, le=30)
    daily_request_limit: int = Field(default=6, ge=1, le=1000)
    monthly_request_limit: int = Field(default=180, ge=1, le=10000)
    results_per_page: int = Field(default=10, ge=1, le=20)
    max_boards: int = Field(default=5, ge=1, le=20)
    publisher_max_requests: int = Field(default=15, ge=1, le=60)
    publisher_runtime_seconds: float = Field(default=90.0, gt=0, le=300)
    interval_seconds: float = Field(default=1.1, ge=1.0, le=60)
    max_runtime_seconds: float = Field(default=180.0, gt=0, le=600)
    freshness: Literal["pd", "pw", "pm", "py"] | None = None

    @model_validator(mode="after")
    def country_name(self):
        if self.country is not None:
            self.country = normalize_country(self.country)
        return self


class RemotiveSettings(ArbeitnowSettings):
    enabled: bool = False
    max_pages: Literal[1] = 1
    max_requests: Literal[1] = 1
    attempts: Literal[1] = 1
    limit: int = Field(default=100, ge=1, le=1000)
    refresh_hours: int = Field(default=24, ge=6, le=168)


class Providers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    arbeitnow: ArbeitnowSettings = Field(default_factory=ArbeitnowSettings)
    himalayas: HimalayasSettings = Field(default_factory=HimalayasSettings)
    brave: BraveSettings = Field(default_factory=BraveSettings)
    remotive: RemotiveSettings = Field(default_factory=RemotiveSettings)


class RegistrationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = True
    max_new_boards_per_run: int = Field(default=20, ge=0, le=100)
    max_requests: int = Field(default=60, ge=1, le=300)
    max_runtime_seconds: float = Field(default=120.0, gt=0, le=600)
    interval_seconds: float = Field(default=1.0, ge=0.5, le=60)
    max_response_bytes: int = Field(default=2_000_000, ge=1024, le=10_000_000)


class CompanySettings(BaseModel):
    """Separate opt-in company searches; Brave account budgets remain shared."""

    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = False
    engineering_domains: list[str] = Field(default_factory=list, max_length=30)
    max_sites: int = Field(default=5, ge=1, le=20)
    max_careers_pages: int = Field(default=2, ge=1, le=5)
    max_site_requests: int = Field(default=30, ge=1, le=100)
    refresh_hours: int = Field(default=168, ge=24, le=720)

    @model_validator(mode="after")
    def domains_valid(self):
        self.engineering_domains = list(
            dict.fromkeys(" ".join(value.split()) for value in self.engineering_domains)
        )
        if any(not value or len(value) > 100 for value in self.engineering_domains):
            raise ValueError("Engineering domains must contain 1..100 characters")
        return self


class DiscoverySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    providers: Providers = Field(default_factory=Providers)
    registration: RegistrationSettings = Field(default_factory=RegistrationSettings)
    companies: CompanySettings = Field(default_factory=CompanySettings)


def load_settings(path: Path | None) -> DiscoverySettings:
    if path is None:
        return DiscoverySettings()
    with path.open(encoding="utf-8") as stream:
        return DiscoverySettings.model_validate(yaml.safe_load(stream))


def normalize_country(country: str) -> str:
    value = " ".join(country.split())
    # Common ISO aliases; all other countries can be supplied by their full name.
    aliases = {
        "HK": "Hong Kong",
        "US": "United States",
        "GB": "United Kingdom",
        "UK": "United Kingdom",
        "CA": "Canada",
        "AU": "Australia",
        "DE": "Germany",
        "FR": "France",
        "IT": "Italy",
        "ES": "Spain",
        "JP": "Japan",
        "SG": "Singapore",
        "IN": "India",
        "CN": "China",
    }
    value = aliases.get(value.upper(), value)
    if len(value) < 3 or len(value) > 80 or any(ord(c) < 32 for c in value):
        raise ValueError("Use a full country name (or a documented country-code alias)")
    return value


def select_country(settings: DiscoverySettings, country: str | None) -> DiscoverySettings:
    """Explicit CLI geography overrides enabled search providers, never enables billing."""
    if country is None:
        return settings
    value = normalize_country(country)
    data = settings.model_dump()
    if settings.providers.himalayas.enabled:
        data["providers"]["himalayas"].update(mode="search", country=value, worldwide_only=False)
    data["providers"]["brave"]["country"] = value
    return DiscoverySettings.model_validate(data)
