"""On-demand evidence validation; requires no database, compiler, or LLM."""

import json
from pathlib import Path

from job_scout.cv.io import load_model, model_hash
from job_scout.cv.models import CVError, MasterCV, TailoredCV, validate_selection


def add_commands(subcommands):
    command = subcommands.add_parser("cv", help="On-demand CV evidence and content validation")
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
