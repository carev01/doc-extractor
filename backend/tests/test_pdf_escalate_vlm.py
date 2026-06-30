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
async def test_escalate_uses_async_vlm_endpoint_and_page_range(monkeypatch):
    # Regression: escalation must go through the async convert API
    # (POST /v1/convert/source/async), not the legacy synchronous endpoint
    # (/v1/convert/source) which this docling-serve deployment 404s on. The
    # 404 was silently swallowed, making VLM escalation a no-op on every run.
    captured = {}

    async def fake_convert_async(pdf_bytes, **kw):
        captured.update(kw)
        return {"md_content": "## Fixed\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n"}

    # The synchronous convert is gone; patching it would no longer exist.
    assert not hasattr(esc.docling_client, "convert")
    monkeypatch.setattr(esc.docling_client, "convert_async", fake_convert_async)
    out = await esc.escalate_segment(b"%PDF", _seg(p0=4, p1=5))
    # convert_async derives pipeline="vlm" from use_vlm_api — no pipeline kwarg.
    assert "pipeline" not in captured
    assert captured["use_vlm_api"] is True
    assert captured["page_range"] == (5, 6)        # 1-based inclusive
    assert "| 1 | 2 |" in out
    assert out.lstrip().startswith("#")


@pytest.mark.asyncio
async def test_escalate_prepends_missing_heading(monkeypatch):
    async def fake_convert_async(pdf_bytes, **kw):
        return {"md_content": "| a | b |\n| --- | --- |\n| 1 | 2 |\n"}

    monkeypatch.setattr(esc.docling_client, "convert_async", fake_convert_async)
    out = await esc.escalate_segment(b"%PDF", _seg(title="My Table", level=2))
    assert out.lstrip().startswith("## My Table")


@pytest.mark.asyncio
async def test_escalate_falls_back_on_error(monkeypatch):
    async def boom(pdf_bytes, **kw):
        raise dc.DoclingServeError("vlm down")

    monkeypatch.setattr(esc.docling_client, "convert_async", boom)
    out = await esc.escalate_segment(b"%PDF", _seg(md="original body"))
    assert out == "original body"
