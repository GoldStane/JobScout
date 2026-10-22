"""Load the public-board registry without interpreting YAML objects."""

from pathlib import Path

import yaml

from job_scout.models import SourceRegistry


def load_registry(path: Path) -> SourceRegistry:
    with path.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    return SourceRegistry(sources=[]) if data is None else SourceRegistry.model_validate(data)
