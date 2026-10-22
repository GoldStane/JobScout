"""Automatic tailoring, private review artifacts and explicit approval."""

import asyncio
import hashlib
import json
import os
from pathlib import Path

from job_scout.cv.compiler import compile_bundle
from job_scout.cv.io import load_model, model_hash
from job_scout.cv.models import CVError, TailoredCV, validate_selection
from job_scout.cv.render import render
from job_scout.cv.rewriting import PROMPT_REVISION, OllamaProvider, apply_rewrites
from job_scout.cv.tailoring import TailoringLimits, read_description, select_evidence


def write_private(path, content):
    with path.open("w", encoding="utf-8") as stream:
        stream.write(content)
    path.chmod(0o600)


def review_text(master, selected, details):
    lines = [
        "CV tailoring review",
        "===================",
        "",
        "DRAFT: review factual accuracy, omissions and the PDF layout before approval.",
        "",
        "Selected evidence (highest relevance first):",
    ]
    for row in details["selection_reasons"]:
        fact = master.facts()[row["fact_id"]]
        lines.extend(
            [
                f"- {fact.id}: {fact.statement}",
                f"  Source: {fact.provenance}",
                "  Matches: " + ", ".join(row["matched_terms"] or row["matched_words"]),
            ]
        )
    lines.extend(
        [
            "",
            "Explicit vacancy terms without matching verified evidence:",
            ", ".join(details["unmatched_terms"]) or "None detected by the term matcher.",
            "",
            "Omitted facts: " + ", ".join(details["omitted_fact_ids"]),
            "",
            "Wording changes:",
        ]
    )
    for change in details["changes"]:
        lines.extend(
            [
                f"- {change['fact_id']} ({change['status']}): {change['reason']}",
                "  Before: " + change["before"],
                "  Proposed: " + change["proposed"],
                "  Used: " + change["after"],
            ]
        )
    if not details["changes"]:
        lines.append("Original verified wording retained.")
    lines.extend(["", *details["warnings"], ""])
    return "\n".join(lines)


def tailor(args, master):
    if args.output.exists():
        raise CVError("Choose a new output directory for tailoring")
    if not args.template.is_file():
        raise CVError("Fixed template not found; use --template with a trusted .tex.j2 file")
    limits = TailoringLimits(
        max_experience=args.max_experience,
        max_projects=args.max_projects,
        max_bullets_per_entry=args.bullets_per_entry,
        max_total_bullets=args.max_bullets,
        max_skills=args.max_skills,
        max_pages=args.max_pages,
    )
    description, description_hash = read_description(args.description_file)
    selected, details = select_evidence(master, description, description_hash, limits)
    changes = []
    model = None
    if args.provider == "ollama":
        model = args.model or os.environ.get("JOBSCOUT_CV_MODEL")
        provider = OllamaProvider(model, max_tokens=args.llm_max_tokens, timeout=args.llm_timeout)
        refs = [row["fact_id"] for row in details["selection_reasons"]]
        # Only selected fact statements and vacancy text leave this process.
        # Names/contact details, provenance, full profile and templates are omitted.
        proposals = asyncio.run(
            provider.rewrite(
                description,
                [{"fact_id": ref, "statement": master.facts()[ref].statement} for ref in refs],
            )
        )
        changes = apply_rewrites(master, selected, proposals)
    allow_rewrites = args.provider != "offline"
    validate_selection(master, selected, allow_rewrites=allow_rewrites)
    details.update(
        provider=args.provider,
        model=model,
        changes=changes,
        prompt_revision=PROMPT_REVISION if allow_rewrites else None,
        job_description_sha256=description_hash,
        model_limits={
            "max_output_tokens": args.llm_max_tokens,
            "timeout_seconds": args.llm_timeout,
            "max_retries": 0,
        }
        if allow_rewrites
        else None,
    )
    if changes:
        details["warnings"].append(
            "AI wording is a draft; lexical checks do not prove semantic truth."
        )
    report = render(master, selected, args.template, args.output, allow_rewrites=allow_rewrites)
    write_private(args.output / "tailoring.json", json.dumps(details, indent=2))
    write_private(args.output / "review.txt", review_text(master, selected, details))
    report.update(
        provider=args.provider,
        model=model,
        prompt_revision=details["prompt_revision"],
        rewrite_review_required=allow_rewrites,
        tailoring_sha256=hashlib.sha256((args.output / "tailoring.json").read_bytes()).hexdigest(),
    )
    write_private(args.output / "report.json", json.dumps(report, indent=2))
    if not args.no_compile:
        report = compile_bundle(
            args.output, allow_downloads=args.allow_downloads, timeout=args.compile_timeout
        )
    return {
        "tailored": True,
        "provider": args.provider,
        "selected_bullets": len(details["selection_reasons"]),
        "wording_changes": sum(c["status"] == "needs_review" for c in changes),
        "rejected_rewrites": sum(c["status"] == "rejected" for c in changes),
        "unmatched_terms": len(details["unmatched_terms"]),
        "compiler_status": report["compiler_status"],
        "pages": report.get("pages"),
        "application_ready": False,
    }


def approve(directory: Path, master, *, confirmed):
    if not confirmed:
        raise CVError("Inspect review.txt and the PDF, then pass --confirm-reviewed to approve")
    try:
        report = json.loads((directory / "report.json").read_text())
        selected = load_model(directory / "selection.json", TailoredCV)
        validate_selection(
            master, selected, allow_rewrites=report.get("rewrite_review_required", False)
        )
        if report["master_sha256"] != model_hash(master) or report[
            "selection_sha256"
        ] != model_hash(selected):
            raise CVError("Profile or selection changed; regenerate and review before approval")
        if report["compiler_status"] != "passed":
            raise CVError("Compile and verify the PDF before approval")
        for name, key in (
            ("cv.tex", "tex_sha256"),
            ("cv.pdf", "pdf_sha256"),
            ("tailoring.json", "tailoring_sha256"),
        ):
            if hashlib.sha256((directory / name).read_bytes()).hexdigest() != report[key]:
                raise CVError("Generated artifacts changed; regenerate and review before approval")
        report.update(application_ready=True, approval="explicit_user_review")
        write_private(directory / "report.json", json.dumps(report, indent=2))
    except CVError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise CVError("Cannot approve this bundle; check its generated artifacts") from None
    return {"application_ready": True, "approval": "explicit_user_review"}
