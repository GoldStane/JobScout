"""Versioned evidence and content selections. All strings are untrusted plain text."""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class CVError(ValueError):
    """Controlled diagnostic safe to print without private input values."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Fact(StrictModel):
    id: Identifier
    statement: Text
    provenance: Text
    evidence_status: Literal["verified", "uncertain"] = "uncertain"


class Dates(StrictModel):
    start: date | None = None
    end: date | None = None
    ongoing: bool = False

    @model_validator(mode="after")
    def ordered(self):
        if self.start and self.end and self.start > self.end:
            raise ValueError("End precedes start")
        if self.ongoing and self.end:
            raise ValueError("Ongoing entries cannot have an end date")
        return self


class Education(StrictModel):
    id: Identifier
    institution: Text
    degree: Text
    graduation: date | None = None


class Experience(StrictModel):
    id: Identifier
    employer: Text
    title: Text
    dates: Dates = Field(default_factory=Dates)
    facts: list[Fact] = Field(default_factory=list, max_length=30)


class Project(StrictModel):
    id: Identifier
    name: Text
    facts: list[Fact] = Field(default_factory=list, max_length=30)


class Skill(StrictModel):
    id: Identifier
    name: Text
    evidence_ids: list[Identifier] = Field(min_length=1, max_length=20)


class MasterCV(StrictModel):
    schema_version: Literal[1] = 1
    name: Text
    contact: list[Text] = Field(default_factory=list, max_length=4)
    education: list[Education] = Field(default_factory=list, max_length=10)
    experience: list[Experience] = Field(default_factory=list, max_length=30)
    projects: list[Project] = Field(default_factory=list, max_length=30)
    skills: list[Skill] = Field(default_factory=list, max_length=60)

    def facts(self) -> dict[str, Fact]:
        return {fact.id: fact for item in [*self.experience, *self.projects] for fact in item.facts}

    @model_validator(mode="after")
    def identities(self):
        entries = [*self.education, *self.experience, *self.projects, *self.skills]
        facts = [fact for item in [*self.experience, *self.projects] for fact in item.facts]
        ids = [entry.id for entry in [*entries, *facts]]
        if len(ids) != len(set(ids)):
            raise ValueError("Evidence and entry IDs must be globally unique")
        known = self.facts()
        for skill in self.skills:
            if len(skill.evidence_ids) != len(set(skill.evidence_ids)):
                raise ValueError("Duplicate skill evidence")
            if any(ref not in known for ref in skill.evidence_ids):
                raise ValueError("Unknown skill evidence")
        return self


class Bullet(StrictModel):
    text: Text
    evidence_ids: list[Identifier] = Field(min_length=1, max_length=10)


class SelectedEntry(StrictModel):
    id: Identifier
    bullets: list[Bullet] = Field(min_length=1, max_length=6)


class TailoredCV(StrictModel):
    schema_version: Literal[1] = 1
    education: list[Identifier] = Field(default_factory=list, max_length=3)
    experience: list[SelectedEntry] = Field(default_factory=list, max_length=5)
    projects: list[SelectedEntry] = Field(default_factory=list, max_length=5)
    skills: list[Identifier] = Field(default_factory=list, max_length=20)
    max_pages: int = Field(default=1, ge=1, le=3)
    job_description_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = (
        None
    )


def validate_selection(master: MasterCV, selected: TailoredCV) -> list[str]:
    """M1 accepts only verbatim verified facts; rewriting requires later review tooling."""
    facts = master.facts()
    evidence = []
    for name in ("education", "experience", "projects", "skills"):
        known = {item.id: item for item in getattr(master, name)}
        choices = getattr(selected, name)
        ids = [item if isinstance(item, str) else item.id for item in choices]
        if len(ids) != len(set(ids)) or any(ref not in known for ref in ids):
            raise CVError("Selection contains duplicate or unknown entry IDs")
        if name in {"experience", "projects"}:
            for entry in choices:
                own = {fact.id for fact in known[entry.id].facts}
                seen = set()
                for bullet in entry.bullets:
                    if len(bullet.evidence_ids) != 1:
                        raise CVError("M1 bullets must cite exactly one verbatim fact")
                    ref = bullet.evidence_ids[0]
                    if ref not in own or ref in seen:
                        raise CVError("Bullet evidence must be unique and belong to its entry")
                    seen.add(ref)
                    if facts[ref].evidence_status != "verified":
                        raise CVError("Uncertain evidence cannot be rendered")
                    if bullet.text != facts[ref].statement:
                        raise CVError(
                            "M1 rejects rewritten claims; use the verified statement verbatim"
                        )
                    evidence.append(ref)
        elif name == "skills":
            for ref in ids:
                skill = known[ref]
                if any(facts[f].evidence_status != "verified" for f in skill.evidence_ids):
                    raise CVError("Selected skills require verified evidence")
                evidence.extend(skill.evidence_ids)
    if not any((selected.education, selected.experience, selected.projects, selected.skills)):
        raise CVError("Select at least one CV entry")
    return list(dict.fromkeys(evidence))
