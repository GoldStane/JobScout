"""Render trusted fixed templates with escaped, evidence-checked content."""

import hashlib
import json
from pathlib import Path

from job_scout.cv.io import model_hash
from job_scout.cv.models import CVError, validate_selection

ESCAPES = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "%": r"\%",
    "$": r"\$",
    "&": r"\&",
    "#": r"\#",
    "_": r"\_",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def escape_tex(value):
    # Newlines cannot create TeX paragraphs, and control bytes never reach TeX.
    if any(ord(char) < 32 and char not in "\n\r\t" for char in str(value)):
        raise CVError("CV text contains unsupported control characters")
    return "".join(ESCAPES.get(char, char) for char in " ".join(str(value).split()))


def context(master, selected):
    validate_selection(master, selected)
    result = {"name": master.name, "contact": " | ".join(master.contact)}
    expected = [master.name, *master.contact]
    for section in ("education", "experience", "projects"):
        known = {item.id: item for item in getattr(master, section)}
        entries = []
        for choice in getattr(selected, section):
            item = known[choice if isinstance(choice, str) else choice.id]
            entry = item.model_dump(mode="json")
            if section == "education":
                entry["graduation"] = str(item.graduation or "")
                expected.extend([item.institution, item.degree])
                if item.graduation:
                    expected.append(str(item.graduation))
            else:
                entry["bullets"] = [bullet.text for bullet in choice.bullets]
                expected.extend(entry["bullets"])
                if section == "experience":
                    dates = item.dates
                    entry["dates"] = " -- ".join(
                        str(v)
                        for v in (dates.start, "Present" if dates.ongoing else dates.end)
                        if v
                    )
                    expected.extend([item.title, item.employer])
                    expected.extend(
                        str(value)
                        for value in (dates.start, "Present" if dates.ongoing else dates.end)
                        if value
                    )
                else:
                    expected.append(item.name)
            entries.append(entry)
        result[section] = entries
        if entries:
            expected.append(section.title())
    skills = {skill.id: skill.name for skill in master.skills}
    result["skills"] = ", ".join(skills[ref] for ref in selected.skills)
    expected.extend(skills[ref] for ref in selected.skills)
    if selected.skills:
        expected.append("Skills")
    return result, expected


def render(master, selected, template: Path, output: Path):
    try:
        from jinja2 import StrictUndefined, TemplateError
        from jinja2.sandbox import SandboxedEnvironment
    except ImportError:
        raise CVError("Install the optional CV dependencies: pip install -e '.[cv]'") from None
    evidence = validate_selection(master, selected)
    data, expected = context(master, selected)
    try:
        with template.open("rb") as stream:
            raw = stream.read(1_000_001)
        if len(raw) > 1_000_000:
            raise CVError("Template exceeds 1 MB")
        environment = SandboxedEnvironment(
            undefined=StrictUndefined,
            autoescape=False,
            block_start_string=r"\BLOCK{",
            block_end_string="}",
            variable_start_string=r"\VAR{",
            variable_end_string="}",
            comment_start_string=r"\COMMENT{",
            comment_end_string="}",
        )
        environment.filters["tex"] = escape_tex
        tex = environment.from_string(raw.decode()).render(**data)
    except (OSError, UnicodeError, TemplateError):
        raise CVError("Cannot render template; check its path and content slots") from None
    report = {
        "schema_version": 1,
        "master_sha256": model_hash(master),
        "selection_sha256": model_hash(selected),
        "job_description_sha256": selected.job_description_sha256,
        "template_sha256": hashlib.sha256(raw).hexdigest(),
        "tex_sha256": hashlib.sha256(tex.encode()).hexdigest(),
        "selected_evidence_ids": evidence,
        "expected_text": expected,
        "max_pages": selected.max_pages,
        "compiler_status": "not_run",
        "application_ready": False,
        "warnings": ["Manual visual review required; no semantic claim rewriting performed."],
    }
    try:
        output.mkdir(parents=True, exist_ok=False, mode=0o700)
        for name, content in {
            "cv.tex": tex,
            "selection.json": selected.model_dump_json(indent=2),
            "report.json": json.dumps(report, indent=2),
        }.items():
            path = output / name
            with path.open("x", encoding="utf-8") as stream:
                stream.write(content)
            path.chmod(0o600)
    except OSError:
        raise CVError("Cannot create CV output; choose a new writable directory") from None
    return report
