import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.docling_client as dc


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return self._payload


class _Client:
    captured = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _Client.captured = {"url": url, "headers": headers, "json": json}
        return _Resp({"status": "success",
                      "document": {"md_content": "# X", "json_content": {"texts": []}}})


@pytest.mark.asyncio
async def test_convert_posts_expected_request(monkeypatch):
    monkeypatch.setattr(dc.settings, "docling_serve_url", "http://docling.test")
    monkeypatch.setattr(dc.settings, "docling_serve_api_key", "secret")
    monkeypatch.setattr(dc.settings, "pdf_vlm_base_url", "http://router/v1/chat")
    monkeypatch.setattr(dc.settings, "pdf_vlm_api_key", "ork")
    monkeypatch.setattr(dc.settings, "pdf_vlm_model", "qwen/qwen3-vl-32b-instruct")
    monkeypatch.setattr(dc.httpx, "AsyncClient", _Client)

    doc = await dc.convert(b"%PDF-1.4 fake", pipeline="vlm", page_range=(2, 3),
                           use_vlm_api=True)
    assert doc["md_content"] == "# X"

    cap = _Client.captured
    assert cap["url"] == "http://docling.test/v1/convert/source"
    assert cap["headers"]["X-Api-Key"] == "secret"
    body = cap["json"]
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


@pytest.mark.asyncio
async def test_convert_raises_on_error_status(monkeypatch):
    class _ErrClient(_Client):
        async def post(self, url, headers=None, json=None):
            return _Resp({"status": "failure", "errors": ["boom"], "document": None})

    monkeypatch.setattr(dc.settings, "docling_serve_url", "http://docling.test")
    monkeypatch.setattr(dc.settings, "docling_serve_api_key", "secret")
    monkeypatch.setattr(dc.httpx, "AsyncClient", _ErrClient)

    with pytest.raises(dc.DoclingServeError):
        await dc.convert(b"x")
