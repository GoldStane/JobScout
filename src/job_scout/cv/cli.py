"""On-demand evidence validation; requires no database, compiler, or LLM."""

import json
from pathlib import Path

from pydantic import ValidationError

from job_scout.cv.io import load_model, model_hash
from job_scout.cv.models import CVError, MasterCV, TailoredCV, validate_selection


def add_commands(subcommands):
    command = subcommands.add_parser(
        "cv", help="Tailor a factual CV to a vacancy and review the PDF"
    )
    actions = command.add_subparsers(dest="cv_command", required=True)
    validate = actions.add_parser(
        "validate", help="Validate a private master CV and optional selection"
    )
    validate.add_argument("--master", type=Path, required=True)
    validate.add_argument("--input", type=Path, help="Optional TailoredCV JSON/YAML to verify")
    schema = actions.add_parser("schema", help="Print the versioned JSON Schema; no private input")
    schema.add_argument("model", choices=["master", "tailored"])
    render = actions.add_parser("render", help="Render a verified selection into a fixed template")
    render.add_argument("--master", type=Path, required=True)
    render.add_argument("--input", type=Path, required=True)
    render.add_argument("--template", type=Path, default=Path("config/cv.tex.j2"))
    render.add_argument("--output", type=Path, required=True, help="New private output directory")
    compile = actions.add_parser("compile", help="Compile and check a generated CV bundle")
    compile.add_argument("directory", type=Path)
    compile.add_argument(
        "--timeout", type=int, default=60, help="Compiler limit in seconds (1..300)"
    )
    compile.add_argument(
        "--allow-downloads",
        action="store_true",
        help="Explicitly allow Tectonic to download missing TeX resources",
    )
    tailor = actions.add_parser(
        "tailor", help="Automatically select vacancy-relevant evidence and make a PDF"
    )
    tailor.add_argument("--master", type=Path, required=True)
    tailor.add_argument("--description-file", type=Path, required=True)
    tailor.add_argument("--template", type=Path, default=Path("config/cv.tex.j2"))
    tailor.add_argument("--output", type=Path, required=True)
    tailor.add_argument(
        "--provider",
        choices=["offline", "ollama"],
        default="offline",
        help="Offline selection, or opt-in rewriting via local Ollama",
    )
    tailor.add_argument("--model", help="Installed local model; defaults to JOBSCOUT_CV_MODEL")
    tailor.add_argument("--llm-max-tokens", type=int, default=2048)
    tailor.add_argument("--llm-timeout", type=int, default=90)
    tailor.add_argument("--max-experience", type=int, default=3)
    tailor.add_argument("--max-projects", type=int, default=3)
    tailor.add_argument("--bullets-per-entry", type=int, default=3)
    tailor.add_argument("--max-bullets", type=int, default=8)
    tailor.add_argument("--max-skills", type=int, default=12)
    tailor.add_argument("--max-pages", type=int, default=1)
    tailor.add_argument(
        "--no-compile", action="store_true", help="Save LaTeX and review files without a PDF"
    )
    tailor.add_argument(
        "--allow-downloads", action="store_true", help="Allow TeX package downloads only"
    )
    tailor.add_argument("--compile-timeout", type=int, default=60)
    approve = actions.add_parser(
        "approve", help="Record your factual and visual approval of a compiled draft"
    )
    approve.add_argument("directory", type=Path)
    approve.add_argument("--master", type=Path, required=True)
    approve.add_argument("--confirm-reviewed", action="store_true")


def execute(args, parser):
    try:
        if args.cv_command == "compile":
            from job_scout.cv.compiler import compile_bundle

            report = compile_bundle(
                args.directory, allow_downloads=args.allow_downloads, timeout=args.timeout
            )
            print(
                json.dumps(
                    {
                        "compiler_status": report["compiler_status"],
                        "pages": report["pages"],
                        "application_ready": False,
                    }
                )
            )
            return 0
        if args.cv_command == "schema":
            model = MasterCV if args.model == "master" else TailoredCV
            print(json.dumps(model.model_json_schema(), indent=2))
            return 0
        master = load_model(args.master, MasterCV)
        if args.cv_command == "tailor":
            from job_scout.cv.workflow import tailor

            if args.model and args.provider != "ollama":
                raise CVError("Use --provider ollama with --model to request AI rewriting")
            print(json.dumps(tailor(args, master), indent=2))
            return 0
        if args.cv_command == "approve":
            from job_scout.cv.workflow import approve

            print(json.dumps(approve(args.directory, master, confirmed=args.confirm_reviewed)))
            return 0
        if args.cv_command == "render":
            from job_scout.cv.render import render

            selected = load_model(args.input, TailoredCV)
            render(master, selected, args.template, args.output)
            print(
                json.dumps(
                    {"rendered": True, "compiler_status": "not_run", "application_ready": False}
                )
            )
            return 0
        output = {
            "valid": True,
            "schema_version": master.schema_version,
            "master_sha256": model_hash(master),
            "facts": len(master.facts()),
            "uncertain_facts": sum(
                f.evidence_status == "uncertain" for f in master.facts().values()
            ),
        }
        if args.input:
            selected = load_model(args.input, TailoredCV)
            evidence = validate_selection(master, selected)
            output.update(
                selection_sha256=model_hash(selected),
                selected_facts=len(evidence),
                application_ready=False,
            )
        print(json.dumps(output, indent=2))
        return 0
    except CVError as exc:
        parser.exit(2, f"{exc}\n")
    except ValidationError:
        parser.exit(2, "Invalid CV limits; check the documented ranges.\n")
