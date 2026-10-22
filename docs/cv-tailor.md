# Automatic CV tailoring

`cv tailor` reads a vacancy, selects and orders relevant verified evidence from a
private master profile, renders your fixed LaTeX template, and compiles a checked
PDF. You no longer need to write the selection JSON yourself. Optional local AI
rewrites the selected bullets. Neither path changes the template or submits applications.

## Run it

Install the optional Python dependencies and Tectonic once:

```sh
python -m pip install -e '.[cv]'
```

Create `private/master_cv.yaml` using `config/master_cv.example.yaml` as the schema
example. It is a separate evidence profile, not the discovery matching profile.
Your original LaTeX CV is preserved; personal facts have not been imported from it.
Save the vacancy as UTF-8 plain text in `private/vacancy.txt`. Then run:

```sh
job-scout cv tailor \
  --master private/master_cv.yaml \
  --description-file private/vacancy.txt \
  --output output/cvs/company-role
```

This default `--provider offline` path selects and orders facts automatically,
keeping their original wording. It needs no model, account, database or API key.
The default template is your private `config/cv.tex.j2`. Other users can pass
`--template config/cv.example.tex.j2` or copy that example to their own private template.
Always choose a new output directory.

For automatic **AI wording** as well as selection, start your trusted local Ollama
server with cloud features disabled and use an already-installed local model:

```sh
OLLAMA_NO_CLOUD=1 ollama serve
# In another terminal, with the local model already installed:
job-scout cv tailor \
  --master private/master_cv.yaml \
  --description-file private/vacancy.txt \
  --provider ollama --model qwen3.5:9b \
  --output output/cvs/company-role-ai
```

The model name is illustrative; use one installed on your machine or set
`JOBSCOUT_CV_MODEL`. JobScout does not install or pull models. If an Ollama server
is already running, configure/restart that server instead of starting another.
See [Ollama local-only configuration](https://docs.ollama.com/faq#how-do-i-disable-ollamas-cloud-features).
Only `127.0.0.1:11434` is contacted. A metadata preflight must confirm a local GGUF
model before selected fact statements and the description are sent. Cloud models,
remote endpoints, redirects and environment proxies are unsupported. The profile's
name/contact fields, employer headers, provenance and template are not sent;
selected statements themselves may still contain personal information.

The local model returns [schema-constrained JSON](https://ollama.com/blog/structured-outputs).
It can rewrite text only, not change selection IDs or LaTeX. There is no paid API
cost; running the model consumes your own computer's memory and compute. No live
model requests run in CI.

## What you get

- `cv.pdf`: the draft CV, after compilation/page/text checks pass.
- `cv.tex` and `selection.json`: editable content artifacts.
- `review.txt`: selection reasons, omissions, evidence provenance, unmatched terms,
  and before/proposed/used wording for each AI change.
- `tailoring.json`: source-linked term mentions, ranking, budgets and model metadata.
- `report.json`: profile/description/template/selection/PDF hashes and review status.

Example output (counts depend on the vacancy):

```json
{"tailored": true, "provider": "offline", "selected_bullets": 2,
 "wording_changes": 0, "compiler_status": "passed", "pages": 1,
 "application_ready": false}
```

Inspect `review.txt` and every PDF page. When you have checked factual accuracy,
omissions and layout, record your approval explicitly:

```sh
job-scout cv approve output/cvs/company-role \
  --master private/master_cv.yaml --confirm-reviewed
```

Approval checks that the master profile, selection, TeX, PDF and tailoring report
still match the generated hashes and that compilation passed. It records your
review; it is not independent verification of your claims and sends nothing.
Changed artifacts require regeneration and fresh review.

## Evidence and factual limits

Entry/fact/skill IDs must be globally unique. Facts need provenance and an explicit
`verified` state to be selected. Dates are optional and checked for ordering;
unknown dates stay blank. Skills reference verified facts. Education/contact fields
are manually asserted profile data, not independently verified credentials.

Selection uses deterministic technical-term and word overlap, with stable ties.
It ranks experience/project bullets globally, then enforces section and total
budgets. Relevant skills are prioritized by explicit required/preferred mentions.
Education is retained in profile order, up to three entries. Unsupported or
uncertain evidence is omitted; no-overlap input produces a clear error rather
than an invented CV. Unmatched terms mean missing explicit evidence, not proven
inability. The term analyzer is not a comprehensive semantic job/eligibility parser.
It records source sentence/line fragments and does not infer sponsorship or eligibility.

AI rewrites must cite every selected fact exactly once. Changed numbers, added
recognized technologies, changed scope qualifiers and markup are rejected per
bullet and the original statement is retained. Every accepted rewrite remains
flagged for human semantic review: these checks cannot prove truth or detect all
invented responsibilities. Unsupported unrecognized claims may still require
rejection during review. No generated draft is automatically application-ready.
The vacancy is untrusted data, never executable instructions.

## Budgets, errors and privacy

Defaults: three experience entries, three projects, three bullets per entry,
eight bullets total, twelve relevant skills, one page. Configure with
`--max-experience` / `--max-projects` (0..5), `--bullets-per-entry` (1..6),
`--max-bullets` (1..30), `--max-skills` (0..20), and `--max-pages` (1..3).
Descriptions are capped at 40 KB. AI input is capped at 24 KB; one generation
request, no retries, 2,048 output tokens (256..4096 via `--llm-max-tokens`),
90 seconds (1..180 via `--llm-timeout`), and 200 KB per response.
There is no silent fallback from failed AI requests to a claimed AI-generated CV.
Retry explicitly with `--provider offline` if you want to continue without a model.

Compilation is automatic unless `--no-compile` is passed. Tectonic uses cached
resources only; `--allow-downloads` explicitly allows missing TeX package downloads.
`--compile-timeout` defaults to 60 seconds (1..300). There is one compilation
attempt; overflow or missing glyphs preserve editable artifacts and fail clearly.
Reduce lower-priority content with `--max-bullets` and regenerate in a new folder.
Fonts and margins are never silently reduced. The compiler's temporary directory,
untrusted mode, credential-free environment and timeout are not a complete OS
sandbox; use only reviewed local templates. No memory limit is enforced.

All generated folders/files use private permissions (0700/0600). `private/`,
`output/cvs/`, `config/latex.template` and private `config/*.tex.j2` are Git-ignored.
CLI logs contain counts/status, not private descriptions or prompts. Review/report
files contain private text; do not publish them accidentally.

## Template and manual tools

`config/cv.example.tex.j2` adapts the supplied MTecknology template (Michael
Lustfield, CC-BY-4; attribution retained). Fonts, margins, colors and layout macros
are preserved. T1 encoding and guarded pdfTeX Unicode mapping support Tectonic;
`-2px` spacing is expressed as the equivalent `-1.5bp`.
Your original `config/latex.template` remains intact; `config/cv.tex.j2` is the
private parameterized copy. Plain-text slots use `\VAR{value|tex}` and conditional
blocks use `\BLOCK{...}`. Preserve escaping on every slot. Contact details are
plain text. Never accept templates from job listings or model output.

The original manual tools remain available:

```sh
job-scout cv validate --master private/master_cv.yaml
job-scout cv schema master
job-scout cv render --master private/master_cv.yaml --input private/tailored_cv.json \
  --template config/cv.tex.j2 --output output/cvs/manual-version
job-scout cv compile output/cvs/manual-version
```

Manual `validate`/`render` still require verbatim verified facts. To revise an AI
result, correct/add approved wording as factual evidence in the private master
and rerun tailoring. Do not edit generated TeX to bypass evidence checks.

## Validation and next step

Tests cover vacancy-dependent selection, budgets, unknown facts, unsafe rewrites,
source citations, prompt boundaries, bounded/malformed/redirected model responses,
private output, compilation, and hash-bound approval. Run:

```sh
python -m pytest tests/test_cv.py tests/test_cv_tailoring.py
```

The next increment (CV-M3) adds stored job keys and exact supported ATS posting URLs.
This release accepts local description files; it does not fetch arbitrary job pages,
run scheduled tailoring, auto-submit applications or offer a hosted AI provider.
