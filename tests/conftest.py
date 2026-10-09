"""Keep tests offline even if a test accidentally constructs a real HTTP client."""

import httpx
import pytest


@pytest.fixture(autouse=True)
def block_real_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("Real HTTP is forbidden in tests; use MockTransport")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)
