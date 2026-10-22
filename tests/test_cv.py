"""Offline CV evidence validation: no private profiles, database, compiler or LLM."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from job_scout.cv.io import load_model, model_hash
from job_scout.cv.models import CVError, Dates, MasterCV, TailoredCV, validate_selection
from job_scout.main import main

MASTER = Path("config/master_cv.example.yaml")
SELECTED = Path("tests/fixtures/cv/tailored.json")


@pytest.fixture
def master():
    return load_model(MASTER, MasterCV)


@pytest.fixture
def selected():
    return load_model(SELECTED, TailoredCV)


def test_fictional_selection_has_traceable_verified_facts(master, selected):
    assert validate_selection(master, selected) == ["fact_parser", "fact_validation"]
    assert master.education[0].graduation is None
    assert model_hash(master) == model_hash(MasterCV.model_validate(master.model_dump()))


@pytest.mark.parametrize("field", ["education", "experience", "projects", "skills"])
def test_duplicate_entry_ids_rejected(master, field):
    data = master.model_dump()
    data[field].append(data[field][0])
    with pytest.raises(ValidationError):
        MasterCV.model_validate(data)


def test_duplicate_fact_ids_across_entries_rejected(master):
    data = master.model_dump()
    data["projects"][0]["facts"][0]["id"] = "fact_parser"
    with pytest.raises(ValidationError):
        MasterCV.model_validate(data)


@pytest.mark.parametrize(
    "dates",
    [
        {"start": "2025-09-01", "end": "2025-01-01"},
        {"end": "2025-01-01", "ongoing": True},
        {"start": "not-a-date"},
    ],
)
def test_invalid_dates_rejected(dates):
    with pytest.raises(ValidationError):
        Dates.model_validate(dates)


@pytest.mark.parametrize("text", ["Built a trading system.", "Improved throughput by 90%."])
def test_rewrites_and_invented_metrics_rejected(master, selected, text):
    selected.experience[0].bullets[0].text = text
    with pytest.raises(CVError, match="rewritten"):
        validate_selection(master, selected)


@pytest.mark.parametrize("ref", ["missing", "fact_validation"])
def test_evidence_must_belong_to_selected_entry(master, selected, ref):
    selected.experience[0].bullets[0].evidence_ids = [ref]
    with pytest.raises(CVError, match="belong"):
        validate_selection(master, selected)


def test_uncertain_evidence_rejected(master, selected):
    bullet = selected.projects[0].bullets[0]
    bullet.evidence_ids = ["fact_uncertain"]
    bullet.text = master.facts()["fact_uncertain"].statement
    with pytest.raises(CVError, match="Uncertain"):
        validate_selection(master, selected)


def test_unknown_and_uncertain_skill_evidence(master, selected):
    data = master.model_dump()
    data["skills"][0]["evidence_ids"] = ["unknown"]
    with pytest.raises(ValidationError):
        MasterCV.model_validate(data)
    master.skills[0].evidence_ids = ["fact_uncertain"]
    with pytest.raises(CVError, match="verified evidence"):
        validate_selection(master, selected)


@pytest.mark.parametrize("field", ["education", "skills"])
def test_unknown_or_duplicate_selection(master, selected, field):
    setattr(selected, field, ["unknown"])
    with pytest.raises(CVError):
        validate_selection(master, selected)
    setattr(selected, field, [getattr(master, field)[0].id] * 2)
    with pytest.raises(CVError):
        validate_selection(master, selected)


def test_empty_selection_rejected(master):
    with pytest.raises(CVError):
        validate_selection(master, TailoredCV())


@pytest.mark.parametrize("change", [{"name": "x" * 501}, {"schema_version": 2}, {"raw_tex": "x"}])
def test_schema_bounds_and_unknown_fields(master, change):
    with pytest.raises(ValidationError):
        MasterCV.model_validate(master.model_dump() | change)


def test_private_errors_do_not_echo_input(tmp_path, capsys):
    path = tmp_path / "private.yaml"
    path.write_text("name: PRIVATE_SECRET\nunknown: PRIVATE_SECRET\n")
    with pytest.raises(SystemExit) as error:
        main(["cv", "validate", "--master", str(path)])
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert "PRIVATE_SECRET" not in captured.err + captured.out


def test_bounded_loader(tmp_path):
    path = tmp_path / "huge.yaml"
    path.write_bytes(b"x" * 1_000_001)
    with pytest.raises(CVError, match="1 MB"):
        load_model(path, MasterCV)


def test_cli_no_database_or_network(master, monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert main(["cv", "validate", "--master", str(MASTER), "--input", str(SELECTED)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["valid"] is True
    assert result["selected_facts"] == 2
    assert result["uncertain_facts"] == 1
    assert result["application_ready"] is False
    assert master.name not in json.dumps(result)
    assert main(["cv", "schema", "tailored"]) == 0
    assert json.loads(capsys.readouterr().out)["additionalProperties"] is False


def test_fixed_template_golden_and_private_artifacts(master, selected, tmp_path):
    pytest.importorskip("jinja2")
    from job_scout.cv.render import render

    output = tmp_path / "cv"
    report = render(master, selected, Path("config/cv.example.tex.j2"), output)
    assert (output / "cv.tex").read_text() == Path("tests/fixtures/cv/expected.tex").read_text()
    assert report["application_ready"] is False
    assert report["compiler_status"] == "not_run"
    assert report["selected_evidence_ids"] == ["fact_parser", "fact_validation"]
    assert (output / "report.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(CVError, match="new writable"):
        render(master, selected, Path("config/cv.example.tex.j2"), output)


def test_tex_injection_and_template_syntax_are_plain_text(master, selected, tmp_path):
    pytest.importorskip("jinja2")
    from job_scout.cv.render import escape_tex, render

    assert escape_tex("\\{}%$&#_~^") == (
        r"\textbackslash{}\{\}\%\$\&\#\_\textasciitilde{}\textasciicircum{}"
    )
    attack = r"\input{/etc/passwd} {{7*7}} \VAR{name} <script>alert(1)</script>"
    master.name = attack
    render(master, selected, Path("config/cv.example.tex.j2"), tmp_path / "cv")
    tex = (tmp_path / "cv/cv.tex").read_text()
    assert escape_tex(attack) in tex
    assert r"\input{/etc/passwd}" not in tex
    with pytest.raises(CVError):
        escape_tex("control\x00byte")


def test_optional_sections_and_selection_order(master, selected, tmp_path):
    pytest.importorskip("jinja2")
    from job_scout.cv.render import render

    selected.experience = []
    selected.skills.reverse()
    render(master, selected, Path("config/cv.example.tex.j2"), tmp_path / "cv")
    tex = (tmp_path / "cv/cv.tex").read_text()
    assert r"\section{Experience}" not in tex
    assert "Python, C++" in tex
    assert r"\documentclass[letterpaper,10pt]{article}" in tex


def test_pdf_checks_page_count_and_missing_content(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    from job_scout.cv.compiler import check_pdf

    path = tmp_path / "empty.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(path)
    with pytest.raises(CVError, match="text verification"):
        check_pdf(path, ["Expected heading"], 1)
    writer.add_blank_page(width=612, height=792)
    writer.write(path)
    with pytest.raises(CVError, match="page budget"):
        check_pdf(path, [], 1)


def test_compiler_offline_flags_environment_and_overflow(master, selected, tmp_path, monkeypatch):
    pytest.importorskip("jinja2")
    from types import SimpleNamespace

    from job_scout.cv import compiler
    from job_scout.cv.render import render

    output = tmp_path / "cv"
    render(master, selected, Path("config/cv.example.tex.j2"), output)
    monkeypatch.setenv("DATABASE_URL", "PRIVATE_SECRET")
    monkeypatch.setattr(compiler.shutil, "which", lambda name: "/fake/tectonic")

    def run(command, **kwargs):
        assert "--only-cached" in command and "--untrusted" in command
        assert kwargs["timeout"] == 60
        assert "DATABASE_URL" not in kwargs["env"]
        assert kwargs.get("shell", False) is False
        (kwargs["cwd"] / "cv.log").write_text(r"Overfull \hbox")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(compiler.subprocess, "run", run)
    with pytest.raises(CVError, match="overflow"):
        compiler.compile_bundle(output)
    assert not (output / "cv.pdf").exists()
    assert json.loads((output / "report.json").read_text())["compiler_status"] == "failed"


def test_modified_tex_never_compiles(master, selected, tmp_path, monkeypatch):
    pytest.importorskip("jinja2")
    from job_scout.cv import compiler
    from job_scout.cv.render import render

    output = tmp_path / "cv"
    render(master, selected, Path("config/cv.example.tex.j2"), output)
    (output / "cv.tex").write_text("changed")
    monkeypatch.setattr(compiler.shutil, "which", lambda name: "/fake/tectonic")
    monkeypatch.setattr(compiler.subprocess, "run", lambda *a, **kw: pytest.fail("No compiler"))
    with pytest.raises(CVError):
        compiler.compile_bundle(output)


def test_compiler_timeout_preserves_editable_bundle(master, selected, tmp_path, monkeypatch):
    pytest.importorskip("jinja2")
    import subprocess

    from job_scout.cv import compiler
    from job_scout.cv.render import render

    output = tmp_path / "cv"
    render(master, selected, Path("config/cv.example.tex.j2"), output)
    monkeypatch.setattr(compiler.shutil, "which", lambda name: "/fake/tectonic")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("tectonic", 60)

    monkeypatch.setattr(compiler.subprocess, "run", timeout)
    with pytest.raises(CVError, match="timed out"):
        compiler.compile_bundle(output)
    assert (output / "cv.tex").exists()
    assert not (output / "cv.pdf").exists()
    assert json.loads((output / "report.json").read_text())["compiler_status"] == "failed"


def test_missing_compiler_is_explicit(tmp_path, monkeypatch):
    from job_scout.cv import compiler

    monkeypatch.setattr(compiler.shutil, "which", lambda name: None)
    with pytest.raises(CVError, match="not installed"):
        compiler.compile_bundle(tmp_path)
