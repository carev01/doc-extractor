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
    # A docling-serve failure is signalled as None so the caller can trip its
    # circuit breaker rather than treating it as a no-op.
    monkeypatch.setattr(esc, "_extract_page_range", lambda *a: b"%PDF-x")

    async def boom(pdf_bytes, **kw):
        raise dc.DoclingServeError("vlm down")

    monkeypatch.setattr(esc.docling_client, "convert_async", boom)
    assert await esc.escalate_page(b"WHOLE", 0) is None
