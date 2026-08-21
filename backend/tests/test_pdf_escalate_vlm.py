import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.pdf_escalate as esc
import app.services.docling_client as dc


@pytest.mark.asyncio
async def test_escalate_page_extracts_single_page_and_uses_vlm(monkeypatch):
    # escalate_page must extract just the one page into a standalone PDF and send
    # THAT to the VLM pipeline (no whole-doc + page_range, which docling rejects).
    captured = {}

    def fake_extract(pdf_bytes, start1, end1):
        captured["range"] = (start1, end1)
        return b"%PDF-one-page"

    async def fake_convert_async(pdf_bytes, **kw):
        captured["pdf_bytes"] = pdf_bytes
        captured.update(kw)
        return {"md_content": "## Fixed\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n"}

    monkeypatch.setattr(esc, "_extract_page_range", fake_extract)
    monkeypatch.setattr(esc.docling_client, "convert_async", fake_convert_async)

    out = await esc.escalate_page(b"WHOLE-DOC", 4)  # 0-based page 4 → 1-based 5
    assert out is not None
    md, images = out
    assert captured["range"] == (5, 5)                 # single-page range
    assert captured["pdf_bytes"] == b"%PDF-one-page"   # sent the extract
    assert "page_range" not in captured
    assert captured["use_vlm_api"] is True
    assert "| 1 | 2 |" in md
    assert images == []


@pytest.mark.asyncio
async def test_escalate_page_content_addresses_images(monkeypatch):
    b64 = base64.b64encode(b"fake-png-bytes").decode("ascii")
    monkeypatch.setattr(esc, "_extract_page_range", lambda *a: b"%PDF-x")

    async def fake_convert_async(pdf_bytes, **kw):
        return {"md_content": f"# Diagram\n\n![alt](data:image/png;base64,{b64})\n"}

    monkeypatch.setattr(esc.docling_client, "convert_async", fake_convert_async)
    md, images = await esc.escalate_page(b"WHOLE", 0)
    assert len(images) == 1
    assert images[0].filename.endswith(".png")
    assert "data:image" not in md                      # base64 stripped
    assert images[0].filename in md                    # rewritten to <sha>.png


@pytest.mark.asyncio
async def test_escalate_page_returns_none_on_docling_failure(monkeypatch):
    # A persistent docling-serve failure is signalled as None (after exhausting
    # retries) so the caller can trip its circuit breaker.
    monkeypatch.setattr(esc.settings, "pdf_vlm_page_retries", 2)
    monkeypatch.setattr(esc.settings, "pdf_vlm_page_retry_backoff", 0)  # no sleep in test
    monkeypatch.setattr(esc, "_extract_page_range", lambda *a: b"%PDF-x")
    calls = {"n": 0}

    async def boom(pdf_bytes, **kw):
        calls["n"] += 1
        raise dc.DoclingServeError("vlm down")

    monkeypatch.setattr(esc.docling_client, "convert_async", boom)
    assert await esc.escalate_page(b"WHOLE", 0) is None
    assert calls["n"] == 3  # 1 initial + 2 retries before giving up


@pytest.mark.asyncio
async def test_escalate_page_does_not_retry_by_default(monkeypatch):
    # Default pdf_vlm_page_retries == 0: a failed page is attempted exactly ONCE.
    # docling's dominant failure ("tile cannot extend outside image") is
    # deterministic, so retrying just wastes budget; and docling gives no error
    # detail to retry selectively.
    assert esc.settings.pdf_vlm_page_retries == 0
    monkeypatch.setattr(esc, "_extract_page_range", lambda *a: b"%PDF-x")
    calls = {"n": 0}

    async def boom(pdf_bytes, **kw):
        calls["n"] += 1
        raise dc.DoclingServeError("tile cannot extend outside image")

    monkeypatch.setattr(esc.docling_client, "convert_async", boom)
    assert await esc.escalate_page(b"WHOLE", 0) is None
    assert calls["n"] == 1  # no retry


@pytest.mark.asyncio
async def test_escalate_page_recovers_after_transient_failure(monkeypatch):
    # docling often fails a page that converts fine on re-submit; a transient
    # failure must be retried, not counted as a failure that abandons the drain.
    monkeypatch.setattr(esc.settings, "pdf_vlm_page_retries", 2)
    monkeypatch.setattr(esc.settings, "pdf_vlm_page_retry_backoff", 0)
    monkeypatch.setattr(esc, "_extract_page_range", lambda *a: b"%PDF-x")
    calls = {"n": 0}

    async def flaky(pdf_bytes, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise dc.DoclingServeError("task failed")   # first attempt wobbles
        return {"md_content": "## Recovered\n\nreal content"}

    monkeypatch.setattr(esc.docling_client, "convert_async", flaky)
    out = await esc.escalate_page(b"WHOLE", 0)
    assert out is not None
    md, _ = out
    assert "Recovered" in md
    assert calls["n"] == 2  # retried once, then succeeded


# ── Terminal docling failures (the NetWorker "tile cannot extend outside image"
#    class) must be distinguished from a wobbly service ────────────────────────

@pytest.mark.asyncio
async def test_terminal_docling_failure_is_not_retried(monkeypatch):
    """A task docling RAN and failed cannot succeed on resubmit, so the remaining
    attempts must not be spent on it."""
    monkeypatch.setattr(esc, "_extract_page_range", lambda *a: b"%PDF-x")
    monkeypatch.setattr(esc.settings, "pdf_vlm_page_retries", 3)
    monkeypatch.setattr(esc.settings, "pdf_vlm_page_retry_backoff", 0)
    calls = []

    async def fake_convert_async(pdf_bytes, **kw):
        calls.append(1)
        raise dc.DoclingConversionFailed(
            "docling-serve task failed: tile cannot extend outside image")

    monkeypatch.setattr(esc.docling_client, "convert_async", fake_convert_async)

    seen: dict[int, str] = {}
    out = await esc.escalate_page(b"DOC", 7, on_error=lambda p, r: seen.__setitem__(p, r))
    assert out is None
    assert len(calls) == 1, "a terminal failure must be attempted exactly once"
    # …and the reason is handed back rather than left in docling's own log.
    assert 7 in seen and "tile cannot extend outside image" in seen[7]


@pytest.mark.asyncio
async def test_transient_docling_failure_is_still_retried(monkeypatch):
    """The retry path must survive: an unreachable/slow service is worth another go."""
    monkeypatch.setattr(esc, "_extract_page_range", lambda *a: b"%PDF-x")
    monkeypatch.setattr(esc.settings, "pdf_vlm_page_retries", 2)
    monkeypatch.setattr(esc.settings, "pdf_vlm_page_retry_backoff", 0)
    calls = []

    async def fake_convert_async(pdf_bytes, **kw):
        calls.append(1)
        if len(calls) < 3:
            raise dc.DoclingServeError("connection reset")
        return {"md_content": "## Recovered\n"}

    monkeypatch.setattr(esc.docling_client, "convert_async", fake_convert_async)
    out = await esc.escalate_page(b"DOC", 0)
    assert out is not None and "Recovered" in out[0]
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_drain_collects_failure_reasons_per_page(monkeypatch):
    """escalate_low_confidence_pages fills `failures` so the run can say why."""
    monkeypatch.setattr(esc, "_extract_page_range", lambda *a: b"%PDF-x")
    monkeypatch.setattr(esc.settings, "pdf_vlm_page_retries", 0)

    async def fake_convert_async(pdf_bytes, **kw):
        raise dc.DoclingConversionFailed("tile cannot extend outside image")

    monkeypatch.setattr(esc.docling_client, "convert_async", fake_convert_async)

    from app.services.pdf_convert import ConvertedDoc
    # Two pages, both empty → both flagged by score_page.
    converted = ConvertedDoc(
        markdown="\n\n",
        headings=[],
        page_texts=["some text on page one", "some text on page two"],
        table_pages=set(),
        page_line_starts=[0, 1],
    )
    failures: dict[int, str] = {}
    ranges = await esc.escalate_low_confidence_pages(
        b"DOC", converted, only={0, 1}, budget=2, failures=failures)

    assert esc.pages_in_ranges(ranges) == {0, 1}          # both still pending
    assert set(failures) == {0, 1}                         # …and both explained
    assert all("tile cannot extend" in r for r in failures.values())
