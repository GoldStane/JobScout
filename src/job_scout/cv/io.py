"""Bounded private input loading and reproducible content hashes."""

import hashlib
import json
from pathlib import Path

import yaml
from pydantic import ValidationError

from job_scout.cv.models import CVError


def load_model(path: Path, model):
    try:
        with path.open("rb") as stream:
            raw = stream.read(1_000_001)
        if len(raw) > 1_000_000:
            raise CVError("CV input exceeds 1 MB")
        return model.model_validate(yaml.safe_load(raw.decode("utf-8")))
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError, RecursionError):
        raise CVError("Cannot read CV input; check file, schema and evidence IDs") from None


def model_hash(model):
    value = json.dumps(model.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(value.encode()).hexdigest()
