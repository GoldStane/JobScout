from pathlib import Path

import pytest
from pydantic import ValidationError

from job_scout.config import load_registry
from job_scout.models import SourceConfig, SourceRegistry
from job_scout.normalize import html_to_text


def test_example_registry_disabled():
    registry = load_registry(Path("config/companies.example.yaml"))
    assert len(registry.sources) == 3
    assert not any(source.enabled for source in registry.sources)


@pytest.mark.parametrize(
    "changes",
    [
        {"source": "unknown"},
        {"board_id": "../evil"},
        {"board_id": ""},
        {"company": " "},
        {"region": "eu"},
        {"enabled": True, "board_id": "REPLACE_WITH_ID"},
        {"typo": True},
    ],
)
def test_invalid_config(changes):
    data = {"source": "greenhouse", "company": "Example", "board_id": "example"} | changes
    with pytest.raises(ValidationError):
        SourceConfig.model_validate(data)


def test_duplicate_boards_rejected():
    source = SourceConfig(source="lever", company="Example", board_id="example")
    with pytest.raises(ValidationError, match="Duplicate"):
        SourceRegistry(sources=[source, source])


def test_html_is_text_only():
    assert (
        html_to_text(
            "<p>Hello <strong>C++</strong></p><script>evil()</script>"
            "<style>hidden</style><p>Linux &amp; Python</p>"
        )
        == "Hello C++\nLinux & Python"
    )


def test_encoded_html():
    assert html_to_text("&amp;lt;p&amp;gt;C++&amp;lt;/p&amp;gt;", encoded=True) == "C++"


def test_yaml_object_tags_rejected(tmp_path):
    import yaml

    path = tmp_path / "bad.yaml"
    path.write_text("!!python/object:builtins.object {}")
    with pytest.raises(yaml.YAMLError):
        load_registry(path)


def test_canonical_job_requires_aware_timestamps():
    from datetime import datetime

    from job_scout.models import Job

    with pytest.raises(ValidationError, match="timezone"):
        Job(
            source="lever",
            source_job_id="1",
            company="Example",
            company_identifier="example",
            title="Intern",
            application_url="https://example.org/apply",
            source_url="https://example.org/job",
            fetched_at=datetime(2026, 1, 1),
            first_seen_at=datetime(2026, 1, 1),
            last_seen_at=datetime(2026, 1, 1),
        )
