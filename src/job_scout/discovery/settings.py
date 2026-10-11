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


class Providers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    arbeitnow: ArbeitnowSettings = Field(default_factory=ArbeitnowSettings)
    himalayas: HimalayasSettings = Field(default_factory=HimalayasSettings)


class RegistrationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = True
    max_new_boards_per_run: int = Field(default=20, ge=0, le=100)
    max_requests: int = Field(default=60, ge=1, le=300)
    max_runtime_seconds: float = Field(default=120.0, gt=0, le=600)
    interval_seconds: float = Field(default=1.0, ge=0.5, le=60)
    max_response_bytes: int = Field(default=2_000_000, ge=1024, le=10_000_000)


class DiscoverySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    providers: Providers = Field(default_factory=Providers)
    registration: RegistrationSettings = Field(default_factory=RegistrationSettings)


def load_settings(path: Path | None) -> DiscoverySettings:
    if path is None:
        return DiscoverySettings()
    with path.open(encoding="utf-8") as stream:
        return DiscoverySettings.model_validate(yaml.safe_load(stream))
