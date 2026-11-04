"""describe_image: OpenAI-compatible vision call, mocked via httpx.MockTransport."""
import asyncio
import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.services import image_describe
from app.services.image_describe import describe_image

_OK = {"choices": [{"message": {"content": json.dumps({"description": "A thing.", "kind": "other"})}}]}


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """Record retry backoffs instead of sleeping through them."""
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(image_describe, "_sleep", fake_sleep)
    return slept


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _flaky(*responses):
    """A handler answering with each response in turn, then 200 OK."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return responses[calls["n"] - 1] if calls["n"] <= len(responses) else httpx.Response(200, json=_OK)

    return handler, calls


def _run(handler):
    async def run():
        async with _client(handler) as c:
            return await describe_image(b"bytes", None, client=c)
    return asyncio.run(run())


def test_rate_limit_is_retried_until_it_succeeds(monkeypatch, _no_backoff_sleep):
    """OpenRouter 429s bursts of a concurrent drain; one attempt per image let five
    in a row trip the enrich circuit breaker and end the run with thousands pending."""
    monkeypatch.setattr(settings, "image_vlm_api_key", "k")
    handler, calls = _flaky(httpx.Response(429, json={"error": {"message": "Provider returned error"}}),
                            httpx.Response(429, json={"error": {"message": "Provider returned error"}}))
    res = _run(handler)
    assert res is not None and res.text == "A thing."
    assert calls["n"] == 3
    assert len(_no_backoff_sleep) == 2 and _no_backoff_sleep[1] > _no_backoff_sleep[0]


def test_retry_after_header_sets_the_wait(monkeypatch, _no_backoff_sleep):
    monkeypatch.setattr(settings, "image_vlm_api_key", "k")
    handler, _ = _flaky(httpx.Response(429, headers={"Retry-After": "7"}, json={}))
    assert _run(handler) is not None
    assert _no_backoff_sleep == [7.0]


def test_gateway_timeout_is_retried(monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_api_key", "k")
    handler, calls = _flaky(httpx.Response(504, json={"error": {"message": "The operation was aborted"}}))
    assert _run(handler) is not None
    assert calls["n"] == 2


def test_persistent_rate_limit_gives_up(monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_api_key", "k")
    monkeypatch.setattr(settings, "image_vlm_retries", 3, raising=False)
    handler, calls = _flaky(*[httpx.Response(429, json={}) for _ in range(10)])
    assert _run(handler) is None
    assert calls["n"] == 4  # first attempt + 3 retries


def test_client_error_is_not_retried(monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_api_key", "k")
    handler, calls = _flaky(httpx.Response(404, json={"error": {"message": "No endpoints found"}}))
    assert _run(handler) is None
    assert calls["n"] == 1


def test_happy_path_returns_text_and_kind(monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_api_key", "k")
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(
                {"description": "Architecture diagram of the backup proxy.", "kind": "diagram"})}}]
        })

    async def run():
        async with _client(handler) as c:
            return await describe_image(b"\x89PNGfakebytes", "topology", client=c)

    res = asyncio.run(run())
    assert res is not None
    assert res.text == "Architecture diagram of the backup proxy."
    assert res.kind == "diagram"
    # Request carries the image as a base64 data URL in a vision content part.
    content = captured["body"]["messages"][-1]["content"]
    assert any(p.get("type") == "image_url" and p["image_url"]["url"].startswith("data:image/")
               for p in content)


def test_service_error_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_api_key", "k")

    def handler(request):
        return httpx.Response(500, text="upstream error")

    async def run():
        async with _client(handler) as c:
            return await describe_image(b"bytes", None, client=c)

    assert asyncio.run(run()) is None


def test_unknown_kind_falls_back_to_other(monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_api_key", "k")

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"description": "A thing.", "kind": "banana"})}}]})

    async def run():
        async with _client(handler) as c:
            return await describe_image(b"bytes", None, client=c)

    res = asyncio.run(run())
    assert res is not None and res.kind == "other"


def test_missing_api_key_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "image_vlm_api_key", "")
    assert asyncio.run(describe_image(b"bytes", None)) is None


def test_reasoning_disabled_by_default(monkeypatch):
    """Hybrid-thinking models count reasoning against max_tokens; with it on,
    qwen3.8-flash spent the whole 300-token budget thinking on 3 of 8 screenshots."""
    monkeypatch.setattr(settings, "image_vlm_api_key", "k")
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"description": "A thing.", "kind": "other"})}}]})

    async def run():
        async with _client(handler) as c:
            return await describe_image(b"bytes", None, client=c)

    assert asyncio.run(run()) is not None
    assert captured["body"]["reasoning"] == {"enabled": False}


def test_http_error_logs_the_providers_message(monkeypatch, caplog):
    """A bare '404 Not Found' hid that the model had been retired (and later that
    the account's guardrail blocked its replacements) — log OpenRouter's reason."""
    monkeypatch.setattr(settings, "image_vlm_api_key", "k")

    def handler(request):
        return httpx.Response(404, json={"error": {
            "message": "Qwen3 VL 32B Instruct was deprecated on Oct 9, 2026.", "code": 404}})

    async def run():
        async with _client(handler) as c:
            return await describe_image(b"bytes", None, client=c)

    with caplog.at_level("WARNING", logger="app.services.image_describe"):
        assert asyncio.run(run()) is None
    assert "HTTP 404" in caplog.text
    assert "deprecated on Oct 9, 2026" in caplog.text


def test_truncated_by_max_tokens_returns_none(monkeypatch, caplog):
    monkeypatch.setattr(settings, "image_vlm_api_key", "k")

    def handler(request):
        return httpx.Response(200, json={"choices": [{
            "message": {"content": '{"description": "The Performance tab'}, "finish_reason": "length"}]})

    async def run():
        async with _client(handler) as c:
            return await describe_image(b"bytes", None, client=c)

    with caplog.at_level("WARNING", logger="app.services.image_describe"):
        assert asyncio.run(run()) is None
    assert "max_tokens" in caplog.text


def test_empty_description_is_logged(monkeypatch, caplog):
    # An empty answer counts toward the circuit breaker and leaves the image pending,
    # so it must be visible in the run log like every other failure.
    monkeypatch.setattr(settings, "image_vlm_api_key", "k")
    empty = {"choices": [{"message": {"content": json.dumps({"description": "", "kind": "other"})}}]}

    with caplog.at_level("WARNING", logger="app.services.image_describe"):
        assert _run(lambda request: httpx.Response(200, json=empty)) is None
    assert "describe_image failed: empty description" in caplog.text
