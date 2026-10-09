import asyncio
import json
import logging

import httpx
import pytest

from job_scout.notifications.telegram import DeliveryError, TelegramSender

TOKEN = "12345:synthetic_test_token"


def send(outcomes):
    calls, sleeps = [], []

    async def run():
        async def sleep(delay):
            sleeps.append(delay)

        def handler(request):
            calls.append(request)
            outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await TelegramSender(client, TOKEN, "123", sleep=sleep).send(
                "literal <b>& hello"
            )

    return run, calls, sleeps


def success():
    return httpx.Response(200, json={"ok": True, "result": {"message_id": 7, "chat": {"id": 123}}})


def test_success_payload_and_no_token_logging(caplog):
    caplog.set_level(logging.INFO)
    run, calls, _ = send([success()])
    assert asyncio.run(run()) == 7
    payload = json.loads(calls[0].content)
    assert payload["chat_id"] == "123"
    assert payload["text"] == "literal <b>& hello"
    assert "parse_mode" not in payload
    assert payload["link_preview_options"]["is_disabled"]
    assert TOKEN not in caplog.text


def test_429_retries_after_confirmed_rejection():
    run, calls, sleeps = send(
        [httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 2}}), success()]
    )
    assert asyncio.run(run()) == 7
    assert len(calls) == 2
    assert sleeps == [2]


@pytest.mark.parametrize(
    "outcome",
    [
        httpx.ReadTimeout("token could appear here"),
        httpx.Response(500, json={"ok": False}),
        httpx.Response(200, text="invalid"),
        httpx.Response(200, json={"ok": True, "result": {}}),
    ],
)
def test_uncertain_outcomes_never_automatically_retried(outcome):
    run, calls, _ = send([outcome])
    with pytest.raises(DeliveryError) as error:
        asyncio.run(run())
    assert error.value.uncertain
    assert len(calls) == 1
    assert "token could appear here" not in str(error.value)


@pytest.mark.parametrize("status", [400, 401, 403])
def test_rejections_are_retryable_after_fix(status):
    run, calls, _ = send([httpx.Response(status, json={"ok": False, "description": TOKEN})])
    with pytest.raises(DeliveryError) as error:
        asyncio.run(run())
    assert not error.value.uncertain
    assert TOKEN not in str(error.value)
    assert len(calls) == 1


def test_rate_limit_exhaustion_is_finite():
    run, calls, sleeps = send(
        [httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 0}})]
    )
    with pytest.raises(DeliveryError) as error:
        asyncio.run(run())
    assert not error.value.uncertain
    assert len(calls) == 3


def test_discover_only_private_chat_ids():
    async def run():
        def handler(request):
            assert request.method == "GET"
            assert request.url.path.endswith("getUpdates")
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {"message": {"chat": {"id": 123, "type": "private"}}},
                        {"message": {"chat": {"id": -123, "type": "group"}}},
                    ],
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await TelegramSender(client, TOKEN, "0").private_chats()

    assert asyncio.run(run()) == [123]
