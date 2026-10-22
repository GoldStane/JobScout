"""Bounded local Tectonic compilation of generated bundles, with PDF checks."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path

from job_scout.cv.models import CVError


def compact(text):
    return "".join(unicodedata.normalize("NFKC", text).split())


def check_pdf(path, expected, max_pages):
    try:
        from pypdf import PdfReader
    except ImportError:
        raise CVError("Install the optional CV dependencies: pip install -e '.[cv]'") from None
    try:
        reader = PdfReader(path, strict=True)
        count = len(reader.pages)
        if not 1 <= count <= max_pages:
            raise CVError("PDF exceeds the page budget; shorten the selection and render again")
        content = compact(" ".join(page.extract_text() or "" for page in reader.pages))
        if any(compact(value) not in content for value in expected):
            raise CVError("PDF text verification failed; inspect missing content or glyphs")
        return count
    except CVError:
        raise
    except Exception:
        raise CVError("Compiled PDF could not be validated") from None


def compile_bundle(directory: Path, *, allow_downloads=False, timeout=60):
    if not 1 <= timeout <= 300:
        raise CVError("Compiler timeout must be between 1 and 300 seconds")
    executable = shutil.which("tectonic")
    if executable is None:
        raise CVError("Tectonic is not installed; install it to compile the preserved cv.tex")
    try:
        tex = (directory / "cv.tex").read_bytes()
        report = json.loads((directory / "report.json").read_text())
        if hashlib.sha256(tex).hexdigest() != report["tex_sha256"]:
            raise CVError("Generated TeX changed; render the selection again before compiling")
        if (directory / "cv.pdf").exists():
            raise CVError("PDF already exists; render into a new directory before compiling")
        expected, max_pages = report["expected_text"], report["max_pages"]
        if not isinstance(max_pages, int) or not 1 <= max_pages <= 3:
            raise CVError("Invalid page budget")
    except CVError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise CVError("Cannot read generated CV bundle") from None
    try:
        with tempfile.TemporaryDirectory(prefix="jobscout-cv-") as temporary:
            work = Path(temporary)
            (work / "cv.tex").write_bytes(tex)
            command = [executable, "-X", "compile", "--untrusted", "--keep-logs"]
            if not allow_downloads:
                command.append("--only-cached")
            command.append("cv.tex")
            # No application tokens or database credentials are inherited.
            env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ}
            env["TECTONIC_UNTRUSTED_MODE"] = "1"
            with (work / "compiler-output.txt").open("wb") as log:
                try:
                    result = subprocess.run(
                        command,
                        cwd=work,
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=timeout,
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    raise CVError(
                        "CV compilation timed out; editable artifacts are preserved"
                    ) from None
            if result.returncode:
                raise CVError(
                    "CV compilation failed; check template packages and cached TeX resources"
                )
            log = (work / "cv.log").read_text(errors="replace")
            if "Overfull \\hbox" in log or "Overfull \\vbox" in log or "Missing character:" in log:
                raise CVError(
                    "CV has overflow or missing glyphs; shorten content or review the template"
                )
            pages = check_pdf(work / "cv.pdf", expected, max_pages)
            with (directory / "cv.pdf").open("xb") as target:
                target.write((work / "cv.pdf").read_bytes())
            (directory / "cv.pdf").chmod(0o600)
            report.update(
                compiler_status="passed",
                pages=pages,
                compiler="tectonic",
                network_allowed=allow_downloads,
            )
    except (CVError, OSError) as exc:
        report["compiler_status"] = "failed"
        (directory / "report.json").write_text(json.dumps(report, indent=2))
        if isinstance(exc, CVError):
            raise
        raise CVError(
            "CV compiler could not access its files; editable artifacts are preserved"
        ) from None
    (directory / "report.json").write_text(json.dumps(report, indent=2))
    return report
