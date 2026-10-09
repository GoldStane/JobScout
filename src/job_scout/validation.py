"""Offline configuration diagnostics without printing private field values."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from job_scout.config import load_registry
from job_scout.matching.profile import load_profile


def validate_config(profile_path: Path, registry_path: Path) -> int:
    failed = False
    for label, path, loader in [
        ("profile", profile_path, load_profile),
        ("registry", registry_path, load_registry),
    ]:
        try:
            value = loader(path)
        except OSError:
            print(f"{label}: cannot read file; check path and permissions")
            failed = True
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            position = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
            print(f"{label}: invalid YAML{position}; check indentation and quoting")
            failed = True
        except ValidationError as exc:
            failed = True
            for error in exc.errors(include_input=False, include_url=False):
                field = ".".join(map(str, error["loc"]))
                hint = {
                    "list_type": "Use a YAML list, e.g. [item], or null if supported",
                    "extra_forbidden": "Unknown setting; remove it or check its spelling",
                    "literal_error": "Unsupported choice; see config/profile.example.yaml",
                    "int_type": "Use an integer, without quotes",
                }.get(error["type"], "Check the field type and allowed range in the example")
                if field == "education":
                    hint = "Set expected_graduation_date OR expected_graduation_year, not both"
                elif field.endswith("expected_graduation_date"):
                    hint = "Use YYYY-MM-DD, null, or use expected_graduation_year instead"
                print(f"{label}.{field}: {hint} ({error['type']})")
        else:
            print(f"{label}: valid")
            if label == "registry" and not any(source.enabled for source in value.sources):
                print("registry: warning: no sources enabled; run cannot collect jobs")
    return 2 if failed else 0
