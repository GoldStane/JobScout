"""Recognize exact public ATS posting identities without fetching URLs."""

import re
from urllib.parse import urlsplit

from job_scout.discovery.detect import SUPPORTED, DiscoveryError, detect


def posting_identity(value: str) -> tuple[str, str, str, str] | None:
    try:
        candidate = detect(value)
        config = candidate.config
        if config is None or config.source not in SUPPORTED:
            return None
        parts = urlsplit(value).path.strip("/").split("/")
        if config.source == "greenhouse":
            if len(parts) != 3 or parts[1] != "jobs" or not parts[2].isdigit():
                return None
            identifier = parts[2]
        else:
            suffix = "apply" if config.source == "lever" else "application"
            if len(parts) not in {2, 3} or (len(parts) == 3 and parts[2] != suffix):
                return None
            identifier = parts[1]
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", identifier):
                return None
        return config.source, config.region, config.board_id, identifier
    except (DiscoveryError, ValueError, TypeError):
        return None


def canonical_posting_url(value: str) -> str | None:
    identity = posting_identity(value)
    if identity is None:
        return None
    candidate = detect(value)
    middle = "/jobs/" if identity[0] == "greenhouse" else "/"
    return candidate.url + middle + identity[3]
