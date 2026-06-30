import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.docling_client as dc


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _SeqClient:
    """submit (POST) → poll(success) (GET) → result (GET), capturing the submit body."""
    posts = []
    seq = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _SeqClient.posts.append((url, headers, json))
        return _Resp({"task_id": "T1", "task_status": "pending"})

    async def get(self, url, headers=None):
        return _Resp(_SeqClient.seq.pop(0))


@pytest.mark.asyncio
async def test_convert_async_vlm_request_carries_model_api(monkeypatch):
    # use_vlm_api=True must submit pipeline="vlm" plus the vlm_pipeline_model_api
    # block to the async endpoint — the path escalation now uses.
    monkeypatch.setattr(dc.settings, "docling_serve_url", "http://docling.test")
    monkeypatch.setattr(dc.settings, "docling_serve_api_key", "secret")
    monkeypatch.setattr(dc.settings, "docling_serve_poll_interval", 0.0)
    monkeypatch.setattr(dc.settings, "pdf_vlm_base_url", "http://router/v1/chat")
    monkeypatch.setattr(dc.settings, "pdf_vlm_api_key", "ork")
    monkeypatch.setattr(dc.settings, "pdf_vlm_model", "qwen/qwen3-vl-32b-instruct")
    _SeqClient.posts = []
    _SeqClient.seq = [
        {"task_id": "T1", "task_status": "success"},
        {"status": "success", "document": {"md_content": "# X", "json_content": {}}},
    ]
    monkeypatch.setattr(dc.httpx, "AsyncClient", _SeqClient)

    doc = await dc.convert_async(b"%PDF-1.4 fake", page_range=(2, 3), use_vlm_api=True)
    assert doc["md_content"] == "# X"

    url, headers, body = _SeqClient.posts[0]
    assert url == "http://docling.test/v1/convert/source/async"
    assert headers["X-Api-Key"] == "secret"
    src = body["sources"][0]
    assert src["kind"] == "file"
    assert base64.b64decode(src["base64_string"]) == b"%PDF-1.4 fake"
    opts = body["options"]
    assert opts["to_formats"] == ["md", "json"]
    assert opts["pipeline"] == "vlm"
    assert opts["page_range"] == [2, 3]
    assert opts["vlm_pipeline_model_api"]["url"] == "http://router/v1/chat"
    assert opts["vlm_pipeline_model_api"]["headers"]["Authorization"] == "Bearer ork"
    assert opts["vlm_pipeline_model_api"]["params"]["model"] == "qwen/qwen3-vl-32b-instruct"
    assert opts["vlm_pipeline_model_api"]["response_format"] == "markdown"


def test_legacy_sync_convert_is_removed():
    # The synchronous /v1/convert/source endpoint 404s on this docling-serve
    # deployment; the only caller (escalation) now uses convert_async. Guard
    # against the dead helper being reintroduced.
    assert not hasattr(dc, "convert")
