"""Plain-text digest formatting with intact job blocks and application URLs."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from job_scout.matching.scoring import MatchResult
from job_scout.models import Job

MAX_UNITS = 4000  # Conservative UTF-16 budget below Telegram's 4096-character limit.


def job_key(job: Job) -> str:
    identity = [job.source, job.source_region, job.company_identifier, job.source_job_id]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def clipped(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if units(text) <= limit:
        return text
    return text.encode("utf-16-le")[: (limit - 1) * 2].decode("utf-16-le", errors="ignore") + "…"


@dataclass(frozen=True)
class DigestPart:
    text: str
    job_keys: list[str]


def build_digest(
    matches: list[MatchResult],
    *,
    now: datetime,
    timezone: str,
    failures: list[str],
    successful_sources: int,
    maximum_matches: int,
    identity_aliases: dict[str, list[str]] | None = None,
    discovery_preview: bool = False,
) -> list[DigestPart]:
    """Formatting is deliberately plain text; job text is never parsed as markup."""
    date = now.astimezone(ZoneInfo(timezone)).date().isoformat()
    blocks: list[tuple[str, list[str]]] = []
    coverage = f"Coverage: {successful_sources} successful source(s), {len(failures)} failed."
    if failures:
        coverage += "\nReduced coverage. Failed sources:\n" + "\n".join(
            f"- {clipped(failure, 150)}" for failure in failures[:10]
        )
        if len(failures) > 10:
            coverage += f"\nAnd {len(failures) - 10} more; see source-health."
    if not matches:
        coverage += (
            "\nNo new relevant matches from successfully checked sources."
            if successful_sources
            else "\nNo sources were successfully checked; results are unavailable."
        )
    else:
        label = (
            "matching job(s); delivery history not checked"
            if discovery_preview
            else ("new, previously undelivered match(es)")
        )
        coverage += f"\n{min(len(matches), maximum_matches)} {label}."
        if len(matches) > maximum_matches:
            coverage += " Remaining matches can appear in a later digest."
    blocks.append((coverage, []))
    for index, match in enumerate(matches[:maximum_matches], start=1):
        job = match.job
        text = (
            f"{index}. {clipped(job.title, 140)} — {clipped(job.company, 100)}\n"
            f"Location: {clipped(job.location_text or 'unknown', 120)}\n"
            f"Type: {clipped(match.role_type or job.employment_type or 'unknown', 60)} | "
            f"Score: {match.score:.1f}/100 | Eligibility: {match.eligibility.status}\n"
            + "\n".join(f"Why: {clipped(reason, 140)}" for reason in match.reasons[:3])
        )
        # Include all material uncertainties, bounded to keep the original link intact.
        if match.unknowns:
            text += "\nUnknowns: " + clipped("; ".join(match.unknowns), 650)
        if job.application_url is not None:
            text += f"\nApply directly: {job.application_url}"
        else:
            text += f"\nView listing: {job.listing_url or job.source_url}"
        if job.salary_text:
            text += f"\nReported compensation: {clipped(job.salary_text, 160)}"
        if job.timezone_restrictions:
            text += "\nTimezone restrictions: " + clipped(", ".join(job.timezone_restrictions), 160)
        attributions = dict(job.attribution_links)
        if job.attribution:
            attributions[job.attribution] = job.attribution_url
        for name, url in attributions.items():
            text += f"\nSource: {clipped(name, 100)}" + (f" — {url}" if url else "")
        if units(text) > MAX_UNITS - 100:
            raise ValueError("A job block exceeds the Telegram budget; shorten job fields")
        blocks.append((text, (identity_aliases or {}).get(job_key(job), [job_key(job)])))
    chunks: list[DigestPart] = []
    text, keys = "", []
    for block, block_keys in blocks:
        combined = text + ("\n\n" if text else "") + block
        if units(combined) > MAX_UNITS - 100:
            chunks.append(DigestPart(text, keys))
            text, keys = block, block_keys
        else:
            text, keys = combined, keys + block_keys
    if text:
        chunks.append(DigestPart(text, keys))
    return [
        DigestPart(f"JobScout — {date} ({index}/{len(chunks)})\n\n{part.text}", part.job_keys)
        for index, part in enumerate(chunks, start=1)
    ]
