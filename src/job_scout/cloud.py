"""GitHub Actions entry point: private configs, persistent storage, safe summaries."""

import contextlib
import io
import json
import logging
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import get_args
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ValidationError
from sqlalchemy.engine import make_url

from job_scout.config import load_registry
from job_scout.discovery.settings import DiscoverySettings, load_settings
from job_scout.main import main as cli_main
from job_scout.matching.profile import Profile, load_profile
from job_scout.models import SourceRegistry, utc_now

MODES = {"initialize", "migrate", "preview", "send", "status", "health"}


class CloudConfigError(Exception):
    """Configuration error whose message never contains private values."""


def config_error(secret: str, exc: Exception, model: type[BaseModel]) -> str:
    """Report schema locations, never values, unknown keys, messages or YAML excerpts."""
    if isinstance(exc, yaml.YAMLError):
        mark = getattr(exc, "problem_mark", None)
        position = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        return f"{secret}: invalid YAML{position}; check indentation and quoting"
    if isinstance(exc, ValidationError):
        fields = set()
        seen = set()

        def visit(annotation):
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                if annotation in seen:
                    return
                seen.add(annotation)
                for name, field in annotation.model_fields.items():
                    fields.add(name)
                    visit(field.annotation)
            for argument in get_args(annotation):
                visit(argument)

        visit(model)
        details = []
        for error in exc.errors(include_input=False, include_url=False, include_context=False)[:5]:
            location = (
                ".".join(
                    str(part) if isinstance(part, int) or part in fields else "<entry>"
                    for part in error["loc"]
                )
                or "root"
            )
            details.append(location)
        return (
            f"{secret}: invalid configuration at {', '.join(details)} "
            "(list indices start at 0); validate locally against the example YAML"
        )
    return f"{secret}: cannot load configuration; validate locally before updating the secret"


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise CloudConfigError(f"Missing {name}")
    return value


def validate_database(value: str) -> None:
    try:
        url = make_url(value)
    except Exception:
        raise CloudConfigError("Invalid DATABASE_URL") from None
    if url.drivername not in {"postgres", "postgresql", "postgresql+psycopg"}:
        raise CloudConfigError("Cloud runs require persistent PostgreSQL, not SQLite")
    if not all([url.host, url.database, url.username, url.password]):
        raise CloudConfigError("DATABASE_URL must include host, database, username and password")
    if url.query.get("sslmode") not in {"require", "verify-ca", "verify-full"}:
        raise CloudConfigError("DATABASE_URL must enable TLS with sslmode=require or stricter")


def schedule_due(profile: Profile, now: datetime) -> bool:
    if now.tzinfo is None:
        raise ValueError("Schedule time must be timezone-aware")
    local = now.astimezone(ZoneInfo(profile.digest.timezone))
    hour, minute = map(int, profile.digest.send_after.split(":"))
    return (local.hour, local.minute) >= (hour, minute)


def invoke(arguments: list[str]) -> int:
    """Never expose CLI previews or raw exceptions to public Actions logs."""
    output, errors = io.StringIO(), io.StringIO()
    previous_disable = logging.root.manager.disable
    try:
        logging.disable(logging.CRITICAL)
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                code = cli_main(arguments)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 2
            except Exception:
                code = 3
    finally:
        logging.disable(previous_disable)
    labels = {
        0: "completed",
        1: "reduced_coverage",
        2: "configuration_error",
        3: "database_or_runtime_error",
        4: "delivery_needs_attention",
    }
    summary: dict = {"result": labels.get(code, "failed"), "exit_code": code}
    # Status/preview bodies are intentionally omitted, including on public repos.
    try:
        raw = output.getvalue().strip()
        if arguments[0] == "discovery-health":
            rows = json.loads(raw)
            # Fixed provider names and integer counts only; no job/profile content.
            summary["discovery_counts"] = {
                row["provider"]: {
                    key: row[key]
                    for key in ("runs", "failed_or_partial")
                    if type(row.get(key)) is int
                }
                | {
                    key: row.get("totals", {}).get(key, 0)
                    for key in ("requests", "publisher_requests", "inserted")
                    if type(row.get("totals", {}).get(key, 0)) is int
                }
                for row in rows
                if row.get("provider") in {"arbeitnow", "himalayas", "brave", "remotive"}
            }
        elif arguments[0] == "delivery-status":
            rows = json.loads(raw)
            states = Counter(row["status"] for row in rows)
            summary["delivery_counts"] = {
                name: states[name] for name in ["pending", "sending", "sent", "failed", "uncertain"]
            }
        else:
            payload = json.loads(raw.splitlines()[-1])
            if payload.get("status") in {"sent", "already_sent", "dry_run"}:
                summary["status"] = payload["status"]
            if type(payload.get("source_failures")) is int:
                summary["source_failures"] = payload["source_failures"]
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        pass
    print(json.dumps(summary))
    if code:
        print("See docs/deployment.md for this exit code; private diagnostics are suppressed.")
    return code


def main() -> int:
    try:
        mode = os.environ.get("JOBSCOUT_MODE", "preview")
        if mode not in MODES:
            raise CloudConfigError("Invalid JOBSCOUT_MODE")
        event = os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch")
        retry = os.environ.get("JOBSCOUT_RETRY_UNCERTAIN", "false")
        if retry not in {"true", "false"}:
            raise CloudConfigError("Invalid JOBSCOUT_RETRY_UNCERTAIN")
        if retry == "true" and (event != "workflow_dispatch" or mode != "send"):
            raise CloudConfigError("Uncertain retries require manual send mode")
        additional = os.environ.get("JOBSCOUT_ADDITIONAL", "false")
        if additional not in {"true", "false"}:
            raise CloudConfigError("Invalid JOBSCOUT_ADDITIONAL")
        if additional == "true" and (
            event != "workflow_dispatch" or mode not in {"send", "preview"}
        ):
            raise CloudConfigError("Additional digests require manual send or preview mode")
        validate_database(required("DATABASE_URL"))
        if mode in {"initialize", "migrate"}:
            return invoke(["db-init" if mode == "initialize" else "db-migrate"])
        if mode == "health":
            return invoke(["discovery-health"])
        required("TELEGRAM_CHAT_ID")
        if mode == "status":
            return invoke(["delivery-status"])
        if mode == "send":
            required("TELEGRAM_BOT_TOKEN")
        with TemporaryDirectory(prefix="jobscout-config-") as directory:
            paths = {}
            for name, secret in [
                ("companies", "JOBSCOUT_COMPANIES_YAML"),
                ("profile", "JOBSCOUT_PROFILE_YAML"),
                ("discovery", "JOBSCOUT_DISCOVERY_YAML"),
            ]:
                path = Path(directory) / f"{name}.yaml"
                path.touch(mode=0o600)
                value = (
                    required(secret) if name == "profile" else os.environ.get(secret, "").strip()
                )
                fallback = "sources: []" if name == "companies" else "{}"
                path.write_text(value or fallback, encoding="utf-8")
                paths[name] = path
            loaded = {}
            for name, secret, loader, model in [
                ("companies", "JOBSCOUT_COMPANIES_YAML", load_registry, SourceRegistry),
                ("profile", "JOBSCOUT_PROFILE_YAML", load_profile, Profile),
                ("discovery", "JOBSCOUT_DISCOVERY_YAML", load_settings, DiscoverySettings),
            ]:
                try:
                    loaded[name] = loader(paths[name])
                except Exception as exc:
                    raise CloudConfigError(config_error(secret, exc, model)) from None
            profile = loaded["profile"]
            if event == "schedule" and not schedule_due(profile, utc_now()):
                print(json.dumps({"status": "before_send_after"}))
                return 0
            args = ["run", "--config", str(paths["companies"]), "--profile", str(paths["profile"])]
            args.extend(["--discovery-config", str(paths["discovery"])])
            if mode == "preview":
                args.append("--dry-run")
            if retry == "true":
                args.append("--retry-uncertain")
            if additional == "true":
                args.append("--additional")
            return invoke(args)
    except CloudConfigError as exc:
        print(str(exc))
        return 2
    except Exception:
        print("Cloud setup failed; validate configuration locally. Private details suppressed.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
