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
