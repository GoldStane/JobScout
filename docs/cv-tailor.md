# CV Tailor: CV-M1 deterministic foundation

CV tooling is on demand and separate from job discovery, matching, PostgreSQL and
Telegram. This increment validates a master evidence profile, checks a manually
selected CV, renders editable LaTeX and compiles/checks a PDF. It does **not** yet
use an LLM, analyze job requirements, fetch job URLs or automatically select facts.
CV-M2 is the next milestone for reviewed AI text tailoring; CV-M3 adds job acquisition.

## Setup and example

```sh
python -m pip install -e '.[cv]'
job-scout cv validate --master config/master_cv.example.yaml \
  --input tests/fixtures/cv/tailored.json
job-scout cv render --master config/master_cv.example.yaml \
  --input tests/fixtures/cv/tailored.json \
  --template config/cv.example.tex.j2 --output output/cvs/example-run
job-scout cv compile output/cvs/example-run
```

Install Tectonic separately. The compiler uses cached resources only by default.
On the first build, explicitly add `--allow-downloads` to permit downloading TeX
support packages. No CV data is sent to an LLM; package retrieval requests go to
Tectonic's resource service. There is no paid API or recurring cost.
See [Tectonic's compile options](https://tectonic-typesetting.github.io/book/latest/v2cli/compile.html).

A successful render prints:

```json
{"rendered": true, "compiler_status": "not_run", "application_ready": false}
```

Compilation prints the check status and page count. Artifacts are `cv.tex`,
`selection.json`, `report.json`, and, after successful checks, `cv.pdf`. Use a **new
output directory** for each version; existing outputs are not overwritten.
`report.json` contains input/template hashes, selected evidence IDs, expected text,
page budget and compiler status. Treat the entire bundle as private. Generated
folders use mode 0700 and files 0600. `output/cvs/` and `private/` are Git-ignored.

## Evidence and selections

Copy `config/master_cv.example.yaml` to `private/master_cv.yaml` and replace its
fictional facts with your own. All entry/fact/skill IDs must be globally unique.
Facts require provenance and an explicit `verified` state to be selected.
Dates are structured, optional and checked for ordering. Unknown dates stay blank.
Skills reference evidence facts; this checks evidence existence/status, not semantic
proof that a fact demonstrates a skill. Education and contact fields are manually
asserted profile data, not independently verified credentials.

The selection format is demonstrated in `tests/fixtures/cv/tailored.json`. It
selects education/skills by ID and orders experience/projects with cited bullets.
M1 accepts only exact verified statements from the corresponding entry. Rewrites,
unknown facts, uncertain facts and duplicate evidence are rejected. This deliberately
avoids claiming that a citation alone establishes a rewritten claim's truth.
Export schemas with `job-scout cv schema master` or `job-scout cv schema tailored`.
`cv validate` requires neither optional dependencies nor a database.

## Fixed templates

`config/cv.example.tex.j2` is a sanitized adaptation of the supplied MTecknology
resume template (Michael Lustfield, CC-BY-4; attribution retained in the file).
It preserves the supplied layout macros, colors, fonts, margins and section styling.
The pdfTeX-only Unicode mapping is conditionally guarded for Tectonic compatibility.
Explicit T1 encoding preserves Charter under XeTeX; the original `-2px` item
spacing is expressed as the equivalent `-1.5bp`, a unit understood by both engines.
The original `config/latex.template` is retained and Git-ignored because it contains
personal content. `config/cv.tex.j2` is the private local parameterized copy and the
CLI default. The example is the shareable starting point for other templates.

Templates use `\VAR{value|tex}` for escaped plain-text slots and
`\BLOCK{if ...}` / `\BLOCK{for ...}` for bounded model sections. Contact details
are plain text in M1, not active hyperlinks. Content is never re-evaluated as Jinja.
Available context: `name`, `contact`, `education`, `experience`, `projects`, `skills`;
see the example for entry fields. Preserve `|tex` on every content slot.
Templates are **trusted local code** reviewed by the user; never accept templates
from a job listing or model output. The Jinja sandbox and Tectonic untrusted mode
are defense in depth, not a hosted multi-tenant filesystem sandbox.

## Compilation, review and limits

The compiler runs in a temporary directory, without shell interpolation or
application credentials, with a 60-second timeout (`--timeout` permits 1–300 seconds
for initial package preparation). It checks the generated TeX hash,
rejects TeX overflow/missing-glyph diagnostics, verifies PDF page count and expected
text, and keeps editable inputs on failure. It never shrinks fonts or changes
margins to force a fit. Shorten/reselect content and render a new version instead.
The template's dependencies include fullpage, titlesec, enumitem, hyperref,
fancyhdr, fontawesome5, multicol, bookmark, lastpage, ragged2e,
CormorantGaramond, charter and xcolor.

Extracted text and page-count checks do not prove perfect visual layout. Inspect
all pages for clipping, overlap, glyphs and readability. Output always remains
`application_ready: false`; no application submission or approval workflow is
implemented. Compilation is not a full OS sandbox and has no memory limit; only
reviewed local templates are supported. Real compilation is checked locally with a fictional sample; CI uses
mocked compiler tests and synthetic PDFs without downloading TeX resources.

Run `python -m pytest tests/test_cv.py` for schemas, claims, privacy, golden TeX,
injection, optional sections, compiler isolation and PDF validation tests.
