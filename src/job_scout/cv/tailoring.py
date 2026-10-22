"""Deterministic vacancy analysis and bounded evidence selection (no network)."""

import hashlib
import re
from pathlib import Path

from pydantic import Field

from job_scout.cv.models import (
    Bullet,
    CVError,
    SelectedEntry,
    StrictModel,
    TailoredCV,
    validate_selection,
)

REVISION = "cv-tailor-1"
STOP = set(
    "a an and are as at be by for from in into is it of on or our the to we with you your "
    "will have has using used work working experience required preferred must skills ability "
    "knowledge good strong excellent candidate role job team company opportunity responsibilities "
    "requirements qualifications software engineer engineering development develop developed "
    "implement implemented build built seeking looking intern internship graduate".split()
)
TECHNOLOGIES = (
    "C++",
    "C#",
    "Python",
    "Java",
    "JavaScript",
    "TypeScript",
    "Rust",
    "Go",
    "SQL",
    "Linux",
    "CMake",
    "Git",
    "Docker",
    "Kubernetes",
    "AWS",
    "CUDA",
    "FPGA",
    "Verilog",
    "VHDL",
    "embedded",
    "firmware",
    "low latency",
    "machine learning",
    "data analysis",
    "trading",
    "algorithms",
    "data structures",
    "networking",
    "testing",
)


def tokens(text):
    return set(re.findall(r"[a-z0-9]+(?:\+\+|#)?", text.casefold())) - STOP


def contains(text, phrase):
    return re.search(r"(?<![\w+#])" + re.escape(phrase) + r"(?![\w+#])", text, re.I) is not None


class TailoringLimits(StrictModel):
    max_experience: int = Field(default=3, ge=0, le=5)
    max_projects: int = Field(default=3, ge=0, le=5)
    max_bullets_per_entry: int = Field(default=3, ge=1, le=6)
    max_total_bullets: int = Field(default=8, ge=1, le=30)
    max_skills: int = Field(default=12, ge=0, le=20)
    max_pages: int = Field(default=1, ge=1, le=3)


def read_description(path: Path):
    try:
        with path.open("rb") as stream:
            raw = stream.read(40_001)
        if len(raw) > 40_000:
            raise CVError("Job description exceeds 40 KB; provide a shorter plain-text description")
        text = raw.decode("utf-8").strip()
        if not text or "\x00" in text:
            raise CVError("Provide a nonempty UTF-8 plain-text job description")
        return text, hashlib.sha256(raw).hexdigest()
    except (OSError, UnicodeError):
        raise CVError("Cannot read job description; use a UTF-8 plain-text file") from None


def analyze(description, master):
    """Extract explicit mentions and source lines; do not infer unstated eligibility."""
    names = list(dict.fromkeys([*TECHNOLOGIES, *(skill.name for skill in master.skills)]))
    mentions = []
    segments = [
        (number, sentence)
        for number, line in enumerate(description.splitlines(), 1)
        for sentence in re.split(r"(?<=[.!?;])\s+", line)
    ]
    for line_number, line in segments:
        line = line.strip()
        if not line:
            continue
        for name in names:
            if contains(line, name):
                priority = "mentioned"
                if re.search(r"\b(not required|not necessary|no .{0,30} required)\b", line, re.I):
                    priority = "not_required"
                elif re.search(r"\b(preferred|desirable|nice.to.have|bonus)\b", line, re.I):
                    priority = "preferred"
                elif re.search(r"\b(required|must|essential|require)\b", line, re.I):
                    priority = "required"
                start = max(0, line.casefold().find(name.casefold()) - 100)
                mentions.append(
                    {
                        "term": name,
                        "priority": priority,
                        "line": line_number,
                        "quote": line[start : start + 500],
                    }
                )
    return {
        "mentions": mentions[:200],
        "method": "explicit_term_mentions",
        "uncertainties": [
            "Eligibility, sponsorship and unstated requirements are not inferred.",
            "Term mentions are not a complete semantic requirements analysis.",
        ],
    }


def select_evidence(master, description, description_hash, limits: TailoringLimits):
    analysis = analyze(description, master)
    query = tokens(description)
    terms = list(
        dict.fromkeys(
            row["term"] for row in analysis["mentions"] if row["priority"] != "not_required"
        )
    )
    facts = master.facts()
    skills_by_fact = {}
    for skill in master.skills:
        for ref in skill.evidence_ids:
            skills_by_fact.setdefault(ref, []).append(skill.name)
    ranked = []
    for section in ("experience", "projects"):
        for entry in getattr(master, section):
            for fact in entry.facts:
                if fact.evidence_status != "verified":
                    continue
                text = fact.statement + " " + " ".join(skills_by_fact.get(fact.id, []))
                overlap = sorted(tokens(text) & query)
                matched = [term for term in terms if contains(text, term)]
                score = len(overlap) + 3 * len(matched)
                if score:
                    ranked.append(
                        {
                            "section": section,
                            "entry_id": entry.id,
                            "fact_id": fact.id,
                            "score": score,
                            "matched_terms": matched,
                            "matched_words": overlap,
                        }
                    )
    ranked.sort(key=lambda row: -row["score"])  # Stable ties retain master-profile order.
    groups = {"experience": {}, "projects": {}}
    chosen = []
    for row in ranked:
        section, entry_id = row["section"], row["entry_id"]
        entries = groups[section]
        entry_limit = getattr(limits, "max_" + section)
        if entry_id not in entries and len(entries) >= entry_limit:
            continue
        if len(entries.get(entry_id, [])) >= limits.max_bullets_per_entry:
            continue
        if len(chosen) >= limits.max_total_bullets:
            break
        entries.setdefault(entry_id, []).append(row["fact_id"])
        chosen.append(row)
    if not chosen:
        raise CVError(
            "No relevant verified evidence found; enrich the master profile or description"
        )
    skills = []
    for skill in master.skills:
        if not all(facts[ref].evidence_status == "verified" for ref in skill.evidence_ids):
            continue
        if contains(description, skill.name):
            skills.append(skill.id)
    skill_names = {skill.id: skill.name for skill in master.skills}
    skills.sort(
        key=lambda ref: (
            -sum(
                {"required": 3, "preferred": 2, "mentioned": 1, "not_required": 0}[row["priority"]]
                for row in analysis["mentions"]
                if row["term"].casefold() == skill_names[ref].casefold()
            )
        )
    )
    selected = TailoredCV(
        education=[item.id for item in master.education[:3]],
        skills=skills[: limits.max_skills],
        max_pages=limits.max_pages,
        job_description_sha256=description_hash,
        **{
            section: [
                SelectedEntry(
                    id=entry_id,
                    bullets=[Bullet(text=facts[ref].statement, evidence_ids=[ref]) for ref in refs],
                )
                for entry_id, refs in entries.items()
            ]
            for section, entries in groups.items()
        },
    )
    validate_selection(master, selected)
    supported = " ".join(f.statement for f in facts.values() if f.evidence_status == "verified")
    supported += " " + " ".join(
        skill.name
        for skill in master.skills
        if all(facts[ref].evidence_status == "verified" for ref in skill.evidence_ids)
    )
    gaps = [term for term in terms if not contains(supported, term)]
    return selected, {
        "revision": REVISION,
        "analysis": analysis,
        "selection_reasons": chosen,
        "unmatched_terms": gaps,
        "omitted_fact_ids": [ref for ref in facts if ref not in {row["fact_id"] for row in chosen}],
        "limits": limits.model_dump(),
        "warnings": [
            "Unmatched terms indicate missing explicit evidence, not proven incapability."
        ],
    }
