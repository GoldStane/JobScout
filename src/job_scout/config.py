"""Load the public-board registry without interpreting YAML objects."""

from pathlib import Path

import yaml

from job_scout.models import SourceRegistry


def load_registry(path: Path) -> SourceRegistry:
    with path.open(encoding="utf-8") as stream:
        return SourceRegistry.model_validate(yaml.safe_load(stream))
