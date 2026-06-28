# backend/tests/test_pdf_pipeline_integration.py
import os
import sys

import fitz
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.pdf_import as pi
from app.services.pdf_convert import ConvertedDoc


def _outline_pdf() -> bytes:
    doc = fitz.open()
    for _ in range(2):
        doc.new_page()
    doc.set_toc([[1, "Alpha Section", 1], [1, "Beta Section", 1]])
    return doc.tobytes()


@pytest.mark.asyncio
async def test_build_segments_splits_outline_without_bleed(monkeypatch):
    md = "## Alpha Section\n\nAlpha body.\n\n## Beta Section\n\nBeta body.\n"

    async def fake_convert(pdf_bytes):
        return ConvertedDoc(markdown=md, headings=[], page_texts=[md, ""],
                            table_pages=set(), images=[], engine="docling")

    monkeypatch.setattr(pi, "convert_pdf", fake_convert)
    monkeypatch.setattr(pi.settings, "pdf_vlm_escalation_enabled", False)

    segs = await pi.build_segments(_outline_pdf())
    assert [s.title for s in segs] == ["Alpha Section", "Beta Section"]
    assert "Beta" not in segs[0].markdown
    assert "Alpha body." not in segs[1].markdown


@pytest.mark.asyncio
async def test_build_segments_escalates_flagged_only(monkeypatch):
    md = ("## Bad\n\n| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n\n"
          "## Good\n\nfine prose here.\n")

    async def fake_convert(pdf_bytes):
        return ConvertedDoc(markdown=md, headings=[], page_texts=[md, ""],
                            table_pages=set(), images=[], engine="docling")

    monkeypatch.setattr(pi, "convert_pdf", fake_convert)
    monkeypatch.setattr(pi.settings, "pdf_vlm_escalation_enabled", True)
    monkeypatch.setattr(pi.settings, "pdf_vlm_max_pages_per_run", 30)

    calls = []

    async def fake_escalate(pdf_bytes, segment):
        calls.append(segment.title)
        return "## Bad\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n"

    monkeypatch.setattr(pi, "escalate_segment", fake_escalate)

    segs = await pi.build_segments(_outline_pdf())
    assert calls == ["Bad"]
    bad = next(s for s in segs if s.title == "Bad")
    assert "| 1 | 2 |" in bad.markdown and "| 1 | 2 | 3 |" not in bad.markdown
