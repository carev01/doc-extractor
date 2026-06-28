# backend/tests/test_docling_client_async.py
import base64, os, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app.services.docling_client as dc


class _Resp:
    def __init__(self, payload): self._p = payload
    def raise_for_status(self): pass
    def json(self): return self._p


class _SeqClient:
    """Returns submit→poll(started)→poll(success)→result in order."""
    seq = []
    posts = []

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, url, headers=None, json=None):
        _SeqClient.posts.append((url, json))
        return _Resp({"task_id": "T1", "task_status": "pending"})

    async def get(self, url, headers=None):
        return _Resp(_SeqClient.seq.pop(0))


@pytest.mark.asyncio
async def test_convert_async_polls_then_returns_document(monkeypatch):
    monkeypatch.setattr(dc.settings, "docling_serve_url", "http://d.test")
    monkeypatch.setattr(dc.settings, "docling_serve_api_key", "k")
    monkeypatch.setattr(dc.settings, "docling_serve_poll_interval", 0.0)
    _SeqClient.seq = [
        {"task_id": "T1", "task_status": "started", "task_position": 0},
        {"task_id": "T1", "task_status": "success"},
        {"status": "success", "document": {"md_content": "# X", "json_content": {}}},
    ]
    _SeqClient.posts = []
    monkeypatch.setattr(dc.httpx, "AsyncClient", _SeqClient)

    polls = []
    async def on_poll(s): polls.append(s["task_status"])

    doc = await dc.convert_async(b"%PDF", page_break_placeholder=dc._PAGE_BREAK, on_poll=on_poll)
    assert doc["md_content"] == "# X"
    assert polls == ["started", "success"]
    # submit body carried the page-break placeholder option
    _, body = _SeqClient.posts[0]
    assert body["options"]["md_page_break_placeholder"] == dc._PAGE_BREAK
    assert _SeqClient.posts[0][0].endswith("/v1/convert/source/async")


@pytest.mark.asyncio
async def test_convert_async_raises_on_failure(monkeypatch):
    monkeypatch.setattr(dc.settings, "docling_serve_url", "http://d.test")
    monkeypatch.setattr(dc.settings, "docling_serve_api_key", "k")
    monkeypatch.setattr(dc.settings, "docling_serve_poll_interval", 0.0)
    _SeqClient.seq = [{"task_id": "T1", "task_status": "failure"}]
    monkeypatch.setattr(dc.httpx, "AsyncClient", _SeqClient)
    with pytest.raises(dc.DoclingServeError):
        await dc.convert_async(b"%PDF")
