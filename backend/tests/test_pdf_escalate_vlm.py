import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.pdf_escalate as esc
import app.services.docling_client as dc
from app.services.pdf_convert import RenderedSegment


def _seg(md="broken", title="Fixed", level=1, p0=0, p1=0):
    return RenderedSegment(title=title, level=level, path=[title],
                           page_start=p0, page_end=p1, markdown=md, images=[])


@pytest.mark.asyncio
async def test_escalate_extracts_page_range_and_uses_vlm_endpoint(monkeypatch):
    # escalate_segment must extract the segment's pages into a standalone PDF and
    # send THAT (no whole-doc + page_range, which docling rejects as invalid).
    captured = {}

    def fake_extract(pdf_bytes, start1, end1):
        captured["extract"] = (pdf_bytes, start1, end1)
        return b"%PDF-extracted-pages"

    async def fake_convert_async(pdf_bytes, **kw):
        captured["pdf_bytes"] = pdf_bytes
        captured.update(kw)
        return {"md_content": "## Fixed\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n"}

    monkeypatch.setattr(esc, "_extract_page_range", fake_extract)
    monkeypatch.setattr(esc.docling_client, "convert_async", fake_convert_async)

    out = await esc.escalate_segment(b"WHOLE-DOC", _seg(p0=4, p1=5))

    # Extracted the segment's 1-based inclusive page range from the whole doc…
    assert captured["extract"] == (b"WHOLE-DOC", 5, 6)
    # …and sent the extract, not the whole document, with no page_range option.
    assert captured["pdf_bytes"] == b"%PDF-extracted-pages"
    assert "page_range" not in captured
    assert captured["use_vlm_api"] is True
    assert "| 1 | 2 |" in out
    assert out.lstrip().startswith("#")


@pytest.mark.asyncio
async def test_escalate_prepends_missing_heading(monkeypatch):
    monkeypatch.setattr(esc, "_extract_page_range", lambda *a: b"%PDF-x")

    async def fake_convert_async(pdf_bytes, **kw):
        return {"md_content": "| a | b |\n| --- | --- |\n| 1 | 2 |\n"}

    monkeypatch.setattr(esc.docling_client, "convert_async", fake_convert_async)
    out = await esc.escalate_segment(b"WHOLE", _seg(title="My Table", level=2))
    assert out.lstrip().startswith("## My Table")


@pytest.mark.asyncio
async def test_escalate_batches_large_segment_and_stitches(monkeypatch):
    # A segment larger than the VLM batch size must be extracted+converted in
    # batches and stitched — a single large extract is rejected by docling, which
    # is why big sections (e.g. a whole chapter) used to fail escalation wholesale.
    monkeypatch.setattr(esc.settings, "pdf_vlm_batch_pages", 2)
    ranges: list[tuple[int, int]] = []

    def fake_extract(pdf_bytes, s, e):
        ranges.append((s, e))
        return f"pdf-{s}-{e}".encode()

    calls: list[bytes] = []

    async def fake_convert_async(pdf_bytes, **kw):
        calls.append(pdf_bytes)
        return {"md_content": f"chunk[{pdf_bytes.decode()}]"}

    monkeypatch.setattr(esc, "_extract_page_range", fake_extract)
    monkeypatch.setattr(esc.docling_client, "convert_async", fake_convert_async)

    # 0-based pages 0..4 → 1-based 1..5, batch size 2 → (1,2),(3,4),(5,5).
    out = await esc.escalate_segment(b"WHOLE", _seg(p0=0, p1=4, title="Big", level=2))

    assert ranges == [(1, 2), (3, 4), (5, 5)]
    assert len(calls) == 3
    assert "chunk[pdf-1-2]" in out and "chunk[pdf-3-4]" in out and "chunk[pdf-5-5]" in out


@pytest.mark.asyncio
async def test_escalate_returns_none_when_any_batch_fails(monkeypatch):
    # If one batch of a multi-batch segment fails, the whole escalation is a
    # failure (None) — never store a partial body missing some batches.
    monkeypatch.setattr(esc.settings, "pdf_vlm_batch_pages", 2)
    monkeypatch.setattr(esc, "_extract_page_range", lambda *a: b"x")
    n = {"i": 0}

    async def fake_convert_async(pdf_bytes, **kw):
        n["i"] += 1
        if n["i"] == 2:
            raise dc.DoclingServeError("batch 2 down")
        return {"md_content": "ok"}

    monkeypatch.setattr(esc.docling_client, "convert_async", fake_convert_async)
    out = await esc.escalate_segment(b"WHOLE", _seg(p0=0, p1=4))
    assert out is None


@pytest.mark.asyncio
async def test_escalate_returns_none_on_docling_failure(monkeypatch):
    # A docling-serve failure is signalled as None (not the original markdown) so
    # the caller can distinguish a service failure from a no-op improvement.
    monkeypatch.setattr(esc, "_extract_page_range", lambda *a: b"%PDF-x")

    async def boom(pdf_bytes, **kw):
        raise dc.DoclingServeError("vlm down")

    monkeypatch.setattr(esc.docling_client, "convert_async", boom)
    out = await esc.escalate_segment(b"WHOLE", _seg(md="original body"))
    assert out is None
