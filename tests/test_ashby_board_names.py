"""Dotted board names remain literal path components throughout ATS acquisition."""

import asyncio

import httpx
import pytest
from pydantic import ValidationError

from job_scout.discovery.detect import DiscoveryError, detect
from job_scout.discovery.posting_links import posting_identity
from job_scout.models import SourceConfig
from job_scout.sources.ashby import AshbySource
from job_scout.sources.base import BoardHTTP


def test_dotted_ashby_name_detection_and_fetch():
    candidate = detect("https://jobs.ashbyhq.com/example.ai/posting-1")
    config = candidate.config
    assert config.board_id == "example.ai"
    assert candidate.url == "https://jobs.ashbyhq.com/example.ai"
    assert posting_identity(candidate.url + "/posting-1") == (
        "ashby",
        "global",
        "example.ai",
        "posting-1",
    )

    def response(request):
        assert request.url.path == "/posting-api/job-board/example.ai"
        return httpx.Response(200, json={"jobs": []})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            assert await AshbySource(BoardHTTP(client)).fetch_jobs(config) == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "board", [".", "..", "../x", "x/../y", "x..y", ".x", "x.", "x%2Fy", "x?y", "x#y", "x\\y", "x y"]
)
def test_unsafe_board_names_still_rejected(board):
    with pytest.raises(ValidationError):
        SourceConfig(source="ashby", company="Example", board_id=board)


@pytest.mark.parametrize("board", ["%2e%2e", "%2e", "x%2Fy", "x%5Cy", "x..y"])
def test_encoded_unsafe_names_still_rejected(board):
    with pytest.raises(DiscoveryError):
        detect("https://jobs.ashbyhq.com/" + board)
