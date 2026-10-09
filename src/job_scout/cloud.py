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
from zoneinfo import ZoneInfo

from sqlalchemy.engine import make_url

from job_scout.config import load_registry
from job_scout.main import main as cli_main
from job_scout.matching.profile import Profile, load_profile
from job_scout.models import utc_now

MODES = {"initialize", "migrate", "preview", "send", "status"}


class CloudConfigError(Exception):
    """Configuration error whose message never contains private values."""


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
        if arguments[0] == "delivery-status":
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
            ]:
                path = Path(directory) / f"{name}.yaml"
                path.touch(mode=0o600)
                path.write_text(required(secret), encoding="utf-8")
                paths[name] = path
            try:
                registry = load_registry(paths["companies"])
                profile = load_profile(paths["profile"])
            except Exception:
                raise CloudConfigError(
                    "Invalid private YAML; validate locally before updating secrets"
                ) from None
            if not any(source.enabled for source in registry.sources):
                raise CloudConfigError("No enabled sources in JOBSCOUT_COMPANIES_YAML")
            if event == "schedule" and not schedule_due(profile, utc_now()):
                print(json.dumps({"status": "before_send_after"}))
                return 0
            args = ["run", "--config", str(paths["companies"]), "--profile", str(paths["profile"])]
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
