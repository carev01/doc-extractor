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
async def test_escalate_uses_vlm_pipeline_and_page_range(monkeypatch):
    captured = {}

    async def fake_convert(pdf_bytes, **kw):
        captured.update(kw)
        return {"md_content": "## Fixed\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n"}

    monkeypatch.setattr(esc.docling_client, "convert", fake_convert)
    out = await esc.escalate_segment(b"%PDF", _seg(p0=4, p1=5))
    assert captured["pipeline"] == "vlm"
    assert captured["use_vlm_api"] is True
    assert captured["page_range"] == (5, 6)        # 1-based inclusive
    assert "| 1 | 2 |" in out
    assert out.lstrip().startswith("#")


@pytest.mark.asyncio
async def test_escalate_prepends_missing_heading(monkeypatch):
    async def fake_convert(pdf_bytes, **kw):
        return {"md_content": "| a | b |\n| --- | --- |\n| 1 | 2 |\n"}

    monkeypatch.setattr(esc.docling_client, "convert", fake_convert)
    out = await esc.escalate_segment(b"%PDF", _seg(title="My Table", level=2))
    assert out.lstrip().startswith("## My Table")


@pytest.mark.asyncio
async def test_escalate_falls_back_on_error(monkeypatch):
    async def boom(pdf_bytes, **kw):
        raise dc.DoclingServeError("vlm down")

    monkeypatch.setattr(esc.docling_client, "convert", boom)
    out = await esc.escalate_segment(b"%PDF", _seg(md="original body"))
    assert out == "original body"
