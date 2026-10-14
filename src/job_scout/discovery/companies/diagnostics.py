"""Allowlisted diagnostics: never emit search URLs, snippets, credentials or raw errors."""

import re

from job_scout.sources.base import SourceError

ACTIONS = {
    "http_redirect": (
        "Redirect requires review: cross-origin, unsafe, missing destination, loop or hop limit."
    ),
    "http_denied": (
        "Publisher denied access; skip this site and use other candidates. "
        "Do not bypass restrictions."
    ),
    "search_denied": (
        "Search API denied access; check its credentials and subscription permissions."
    ),
    "http_not_found": (
        "The publisher endpoint is unavailable; verify it manually or use another source."
    ),
    "http_rate_limited": "Wait for the provider's quota reset before retrying.",
    "http_server_error": (
        "Publisher is temporarily unavailable; retry later within the request budget."
    ),
    "http_error": "Publisher returned an unsuccessful HTTP status; inspect the status code.",
    "timeout": "Request or runtime deadline expired; retry later before increasing time limits.",
    "transport_error": "Connection failed; check network/TLS availability and retry later.",
    "dns_error": "Public DNS lookup failed; check connectivity and retry later.",
    "unsafe_target": "URL or DNS target failed public-address safety checks; do not disable them.",
    "robots_denied": "Publisher robots policy disallows access; do not bypass it.",
    "robots_delay": "Publisher crawl delay exceeds this run's limit; defer this publisher.",
    "page_opt_out": "Publisher opted out of indexing/link following; do not bypass it.",
    "not_html": "Expected a static HTML publisher page; dynamic/other formats need an adapter.",
    "compressed_response": (
        "Publisher ignored the uncompressed request; bounded decoding support is needed."
    ),
    "response_too_large": "Response exceeded the size cap; inspect before raising the cap.",
    "request_budget": (
        "Request allowance is exhausted; inspect limits and existing usage before retrying."
    ),
    "identity_missing": (
        "No usable organization or site-name identity on the homepage; "
        "manual verification is needed."
    ),
    "identity_ambiguous": (
        "Conflicting Organization metadata; do not register until ownership is resolved."
    ),
    "reciprocal_link_missing": (
        "ATS page did not link back to the official site; ownership needs review."
    ),
    "irrelevant": (
        "Publisher content did not match configured interests; review engineering_domains."
    ),
    "unsupported": (
        "No supported ATS link was found on the inspected pages; manual review is needed."
    ),
    "already_known": (
        "Board is already registered, rejected or examined; existing decisions are preserved."
    ),
    "site_limit": "Site allowance reached; remaining candidates were deferred.",
    "board_limit": "Board validation allowance reached; remaining boards were deferred.",
    "page_limit": "Search page allowance reached; more results may remain.",
    "repeated_page": "Search provider repeated results; pagination stopped.",
    "invalid_search": "Configure engineering_domains or profile roles/skills before searching.",
    "validation_failed": (
        "ATS endpoint or posting identity could not be verified; inspect company candidates."
    ),
    "invalid_board_identifier": (
        "Observed ATS URL has an invalid board identifier; inspect the URL parser."
    ),
    "https_required": "Publisher URL is not HTTPS; an explicitly verified HTTPS link is required.",
    "identity_conflict": (
        "Publisher identity conflicts with the stored company; manual review is required."
    ),
    "verification_error": (
        "Verification failed without a recognized safe diagnostic; inspect the adapter."
    ),
    "registered": "Verified board is available to subsequent monitor runs; no job alert was sent.",
}
STAGES = {
    "search",
    "lead",
    "robots",
    "homepage",
    "identity",
    "careers",
    "relevance",
    "ats_page",
    "ats_identity",
    "ats_api",
    "registration",
    "limits",
    "runtime",
}


class PageFetchError(SourceError):
    def __init__(self, stage, cause):
        super().__init__("Publisher page fetch failed")
        self.stage = stage
        self.diagnostic = classify(cause)


def classify(error) -> dict:
    message = str(error)
    status = re.match(r"(?:Brave )?HTTP (\d{3})(?:\b)", message)
    if status:
        number = int(status[1])
        code = (
            "http_redirect"
            if 300 <= number < 400
            else "http_denied"
            if number in {401, 403}
            else "http_not_found"
            if number == 404
            else "http_rate_limited"
            if number == 429
            else "http_server_error"
            if number >= 500
            else "http_error"
        )
        return {"code": code, "http_status": number}
    rules = [
        ("valid board identifier", "invalid_board_identifier"),
        ("require HTTPS", "https_required"),
        ("Conflicting", "identity_conflict"),
        ("Malformed careers URL", "unsafe_target"),
        ("URL must be", "unsafe_target"),
        ("without credentials", "unsafe_target"),
        ("Nonstandard ports", "unsafe_target"),
        ("private/internal hosts", "unsafe_target"),
        ("public ASCII hostname", "unsafe_target"),
        ("robots policy", "robots_denied"),
        ("crawl delay", "robots_delay"),
        ("opts out", "page_opt_out"),
        ("not HTML", "not_html"),
        ("Compressed response", "compressed_response"),
        ("size limit", "response_too_large"),
        ("DNS lookup", "dns_error"),
        ("non-public", "unsafe_target"),
        ("no public address", "dns_error"),
        ("Timeout", "timeout"),
        ("time budget", "timeout"),
        ("Transport failure:", "transport_error"),
        ("budget exhausted", "request_budget"),
        ("quota exhausted", "request_budget"),
        ("Retry-After", "http_rate_limited"),
    ]
    return {
        "code": next((code for phrase, code in rules if phrase in message), "verification_error")
    }


def safe_quota(value):
    """Only fixed scopes, nonnegative counters and ISO timestamps leave the process."""
    from datetime import datetime

    if not isinstance(value, dict) or value.get("scope") not in {
        "daily",
        "monthly",
        "per_run",
        "provider",
    }:
        return None
    result = {"scope": value["scope"]}
    for key in ("used", "limit"):
        number = value.get(key)
        result[key] = number if type(number) is int and number >= 0 else None
    try:
        timestamp = datetime.fromisoformat(value.get("reset_at"))
        result["reset_at"] = timestamp.isoformat() if timestamp.tzinfo else None
    except (TypeError, ValueError):
        result["reset_at"] = None
    return result
