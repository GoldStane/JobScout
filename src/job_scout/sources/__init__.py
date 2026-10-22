"""Public employer job-board adapters and their shared dispatch registry."""

from job_scout.sources.ashby import AshbySource
from job_scout.sources.base import BoardHTTP, JobSource, SourceError
from job_scout.sources.greenhouse import GreenhouseSource
from job_scout.sources.lever import LeverSource

SOURCE_TYPES = {"ashby": AshbySource, "greenhouse": GreenhouseSource, "lever": LeverSource}


def get_source(provider: str, http: BoardHTTP) -> JobSource:
    try:
        adapter = SOURCE_TYPES[provider]
    except KeyError:
        raise SourceError(
            "Unsupported provider; disable this source until an adapter exists"
        ) from None
    return adapter(http)
