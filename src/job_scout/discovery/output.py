"""Readable terminal discovery reports without Telegram chunking or JSON escapes."""

import re
import textwrap


def clean(value: object) -> str:
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(value))


def render_discovery(report: dict) -> str:
    lines = ["JobScout · Job discovery", ""]
    if report.get("status") == "disabled":
        return "\n".join(lines + ["All discovery providers are disabled in discovery settings."])
    mode = "Preview only — no changes saved" if report["dry_run"] else "Saved to database"
    lines += [
        mode,
        f"Provider: {report['provider']} · Pages: {report['pages']} · "
        f"Requests: {report['requests']}",
        f"Jobs: {report['fetched_jobs']} fetched · {report['skipped_jobs']} skipped",
    ]
    for provider in report.get("providers", []):
        lines.append(
            f"  {provider['provider']}: {provider['unique_jobs']} unique observations · "
            f"{provider['matching_jobs']} matches · {provider['requests']} requests · "
            f"{provider['expired']} expired"
        )
        if provider["error"]:
            lines.append("    Error: " + clean(provider["error"]))
    counts = report["ingestion"]
    if counts:
        lines.append(
            f"Storage: {counts['inserted']} new · {counts['updated']} updated · "
            f"{counts['unchanged']} unchanged"
        )
    if report["page_limit_reached"]:
        lines.append("Coverage: configured page limit reached; this is a bounded feed sample.")
    if report["error"]:
        lines.append(f"Reduced coverage: {clean(report['error'])}")
    registration = report["ats_registration"]
    lines += [
        "",
        f"ATS boards · {registration['requests']} validation request(s) · "
        f"{registration['deferred']} deferred",
    ]
    if not registration["outcomes"]:
        lines.append("  No board changes.")
    for outcome in registration["outcomes"]:
        lines.append(
            f"  {clean(outcome['state'])}: {clean(outcome['board'])} "
            f"({clean(outcome['validation'])})"
        )
        if outcome.get("reason"):
            lines.append("    " + clean(outcome["reason"]))
    if registration["error"]:
        lines.append("  " + clean(registration["error"]))
    matches = report["matches"]
    lines += [
        "",
        f"Matches · showing {len(matches)} of {report['matching_jobs']}",
        "Preview only; delivery history is not checked and no messages are sent.",
    ]
    for index, match in enumerate(matches, 1):
        job = match["job"]
        lines += [
            "",
            f"{index}. {clean(job['title'])} — {clean(job['company'])}",
            f"   {match['score']:.1f}/100 · {clean(job['location_text'] or 'Location unknown')} · "
            f"{clean(match['role_type'] or job['employment_type'] or 'Type unknown')}",
            f"   Eligibility: {clean(match['eligibility']['status'])}",
        ]
        for reason in match["reasons"]:
            lines.extend(
                textwrap.wrap(
                    clean(reason),
                    width=100,
                    initial_indent="   Why: ",
                    subsequent_indent="        ",
                )
            )
        if match["unknowns"]:
            lines.append("   Unknowns:")
            for unknown in match["unknowns"]:
                lines.extend(
                    textwrap.wrap(
                        clean(unknown),
                        width=100,
                        initial_indent="     - ",
                        subsequent_indent="       ",
                    )
                )
        label = "Apply directly" if job["application_url"] else "View listing"
        url = job["application_url"] or job["listing_url"] or job["source_url"]
        lines.append(f"   {label}: {clean(url)}")
        if job.get("salary_text"):
            lines.append("   Reported compensation: " + clean(job["salary_text"]))
        if job.get("timezone_restrictions"):
            lines.append(
                "   Timezone restrictions: " + clean(", ".join(job["timezone_restrictions"]))
            )
        attributions = dict(job.get("attribution_links", {}))
        if job["attribution"]:
            attributions[job["attribution"]] = job["attribution_url"]
        for name, url in attributions.items():
            lines.append(f"   Source: {clean(name)} — {clean(url)}")
    if not matches:
        lines += ["", "No jobs met this profile's matching threshold in the fetched sample."]
    return "\n".join(lines)
