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


class _FlakyClient:
    """submit ok; first poll GET raises 502, then poll(success), then result."""
    def __init__(self, *a, **k):
        self.gets = 0
        self.seq = [
            {"task_id": "T1", "task_status": "success"},
            {"status": "success", "document": {"md_content": "# OK", "json_content": {}}},
        ]
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, headers=None, json=None):
        return _Resp({"task_id": "T1", "task_status": "pending"})
    async def get(self, url, headers=None):
        self.gets += 1
        if self.gets == 1:
            import httpx
            raise httpx.HTTPError("502 Bad Gateway")
        return _Resp(self.seq.pop(0))


@pytest.mark.asyncio
async def test_convert_async_tolerates_transient_poll_error(monkeypatch):
    monkeypatch.setattr(dc.settings, "docling_serve_url", "http://d.test")
    monkeypatch.setattr(dc.settings, "docling_serve_api_key", "k")
    monkeypatch.setattr(dc.settings, "docling_serve_poll_interval", 0.0)
    # Exercise the retry window explicitly rather than depending on the ambient
    # default (CI pins it to 0 so accidental real-docling calls fail fast).
    monkeypatch.setattr(dc.settings, "docling_serve_transient_window", 120.0)
    monkeypatch.setattr(dc.httpx, "AsyncClient", _FlakyClient)
    doc = await dc.convert_async(b"%PDF")
    assert doc["md_content"] == "# OK"   # recovered despite the transient 502


class _SustainedOutageClient:
    """submit ok; the poll GET 502s `fail_n` times (a worker restart), then
    poll(success) then result — exercising the time-windowed retry."""
    fail_n = 8

    def __init__(self, *a, **k):
        self.gets = 0
        self.seq = [
            {"task_id": "T1", "task_status": "success"},
            {"status": "success", "document": {"md_content": "# BACK", "json_content": {}}},
        ]
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, headers=None, json=None):
        return _Resp({"task_id": "T1", "task_status": "pending"})
    async def get(self, url, headers=None):
        self.gets += 1
        if self.gets <= _SustainedOutageClient.fail_n:
            import httpx
            raise httpx.HTTPError("502 Bad Gateway")
        return _Resp(self.seq.pop(0))


@pytest.mark.asyncio
async def test_convert_async_rides_out_sustained_outage(monkeypatch):
    # 8 consecutive 502s (more than the old fixed 5-retry cap) within the
    # transient window must NOT abandon the conversion — a worker restart should
    # be ridden out rather than dumping the document to the pymupdf fallback.
    monkeypatch.setattr(dc.settings, "docling_serve_url", "http://d.test")
    monkeypatch.setattr(dc.settings, "docling_serve_api_key", "k")
    monkeypatch.setattr(dc.settings, "docling_serve_poll_interval", 0.0)
    monkeypatch.setattr(dc.settings, "docling_serve_transient_window", 120.0)
    monkeypatch.setattr(dc.httpx, "AsyncClient", _SustainedOutageClient)
    doc = await dc.convert_async(b"%PDF")
    assert doc["md_content"] == "# BACK"


class _FlakySubmitClient:
    """The submit POST 502s once (worker mid-restart), then succeeds; poll+result ok."""
    def __init__(self, *a, **k):
        self.posts = 0
        self.seq = [
            {"task_id": "T1", "task_status": "success"},
            {"status": "success", "document": {"md_content": "# OK", "json_content": {}}},
        ]
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, headers=None, json=None):
        self.posts += 1
        if self.posts == 1:
            import httpx
            raise httpx.HTTPError("502 Bad Gateway")
        return _Resp({"task_id": "T1", "task_status": "pending"})
    async def get(self, url, headers=None):
        return _Resp(self.seq.pop(0))


@pytest.mark.asyncio
async def test_convert_async_retries_transient_submit_error(monkeypatch):
    # A blip on the submit POST (not just polling) must also be ridden out —
    # otherwise a batch landing on a restart dumps the whole document to pymupdf.
    monkeypatch.setattr(dc.settings, "docling_serve_url", "http://d.test")
    monkeypatch.setattr(dc.settings, "docling_serve_api_key", "k")
    monkeypatch.setattr(dc.settings, "docling_serve_poll_interval", 0.0)
    monkeypatch.setattr(dc.settings, "docling_serve_transient_window", 120.0)
    monkeypatch.setattr(dc.httpx, "AsyncClient", _FlakySubmitClient)
    doc = await dc.convert_async(b"%PDF")
    assert doc["md_content"] == "# OK"


@pytest.mark.asyncio
async def test_convert_async_gives_up_after_transient_window(monkeypatch):
    # A window of 0 means the very first transient error is terminal — the retry
    # is bounded, so a genuinely-down service still surfaces as DoclingServeError
    # (→ pymupdf fallback) rather than hanging.
    monkeypatch.setattr(dc.settings, "docling_serve_url", "http://d.test")
    monkeypatch.setattr(dc.settings, "docling_serve_api_key", "k")
    monkeypatch.setattr(dc.settings, "docling_serve_poll_interval", 0.0)
    monkeypatch.setattr(dc.settings, "docling_serve_transient_window", 0.0)
    monkeypatch.setattr(dc.httpx, "AsyncClient", _FlakyClient)  # poll GET 502s first
    with pytest.raises(dc.DoclingServeError):
        await dc.convert_async(b"%PDF")
