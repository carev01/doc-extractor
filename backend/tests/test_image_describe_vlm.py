"""describe_image: OpenAI-compatible vision call, mocked via httpx.MockTransport."""
import asyncio
import json
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.services.image_describe import describe_image


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


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
