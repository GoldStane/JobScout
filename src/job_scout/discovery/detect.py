"""Conservative URL recognition; a recognized slug is not a verified board."""

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit, urlunsplit

from job_scout.models import SourceConfig

HOSTS = {
    "boards.greenhouse.io": ("greenhouse", "global", "job-boards.greenhouse.io"),
    "job-boards.greenhouse.io": ("greenhouse", "global", "job-boards.greenhouse.io"),
    "jobs.lever.co": ("lever", "global", "jobs.lever.co"),
    "jobs.eu.lever.co": ("lever", "eu", "jobs.eu.lever.co"),
    "jobs.ashbyhq.com": ("ashby", "global", "jobs.ashbyhq.com"),
    "careers.smartrecruiters.com": ("smartrecruiters", "global", "careers.smartrecruiters.com"),
}
SUPPORTED = {"greenhouse", "lever", "ashby"}


class DiscoveryError(ValueError):
    """Safe, actionable discovery input error."""


@dataclass(frozen=True)
class Candidate:
    url: str
    config: SourceConfig | None

    @property
    def identity(self) -> str:
        if self.config:
            return f"{self.config.source}:{self.config.region}:{self.config.board_id}"
        return self.url


def detect(value: str, *, name: str | None = None) -> Candidate:
    if len(value) > 2048 or any(ord(c) < 33 for c in value) or "\\" in value:
        raise DiscoveryError(
            "URL must be at most 2048 characters with no whitespace or backslashes"
        )
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower()
        port = url.port
    except ValueError:
        raise DiscoveryError("Malformed careers URL") from None
    if (
        url.scheme not in {"http", "https"}
        or not host
        or url.username is not None
        or url.password is not None
    ):
        raise DiscoveryError("Use an HTTP(S) careers URL without credentials")
    if port not in {None, 80 if url.scheme == "http" else 443}:
        raise DiscoveryError("Nonstandard ports are not permitted")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if (
        address is not None
        or "." not in host
        or host.endswith(
            (".local", ".internal", ".localhost", ".lan", ".home", ".test", ".invalid")
        )
    ):
        raise DiscoveryError("IP literals and private/internal hosts are not permitted")
    if not re.fullmatch(r"[a-z0-9.-]+", host) or host.endswith("."):
        raise DiscoveryError("Use a public ASCII hostname without a trailing dot")
    if host not in HOSTS:
        # Never fetch arbitrary careers pages in this milestone. Remove tracking/secrets.
        return Candidate(urlunsplit((url.scheme, host, url.path or "/", "", "")), None)
    provider, region, canonical_host = HOSTS[host]
    board = unquote(url.path.lstrip("/").split("/", 1)[0])
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", board) or board.startswith("REPLACE_WITH_"):
        raise DiscoveryError("URL does not contain a valid board identifier")
    return Candidate(
        f"https://{canonical_host}/{board}",
        SourceConfig(source=provider, region=region, board_id=board, company=name or board),
    )
