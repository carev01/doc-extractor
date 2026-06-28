# backend/tests/test_pdf_pipeline_integration.py
import os
import sys

import fitz
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.pdf_import as pi
from app.services.pdf_convert import ConvertedDoc


def _outline_pdf() -> bytes:
    # Both sections start on page 1 (a shared page).
    doc = fitz.open()
    for _ in range(2):
        doc.new_page()
    doc.set_toc([[1, "Alpha Section", 1], [1, "Beta Section", 1]])
    return doc.tobytes()


def _two_page_outline_pdf() -> bytes:
    # Each section owns its own page (page 1, page 2) — both exclusive.
    doc = fitz.open()
    for _ in range(2):
        doc.new_page()
    doc.set_toc([[1, "Bad", 1], [1, "Good", 2]])
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
    # Bad (page 0) and Good (page 1) each own their page → both eligible; only
    # Bad is flagged (ragged table), so only Bad is escalated.
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

    segs = await pi.build_segments(_two_page_outline_pdf())
    assert calls == ["Bad"]
    bad = next(s for s in segs if s.title == "Bad")
    assert "| 1 | 2 |" in bad.markdown and "| 1 | 2 | 3 |" not in bad.markdown


@pytest.mark.asyncio
async def test_build_segments_skips_escalation_on_shared_page(monkeypatch):
    # Alpha and Beta both live on page 1 (shared). Even though Alpha is flagged
    # (ragged table), escalation must SKIP it — re-converting the shared page via
    # the VLM page_range would pull Beta's content in and reintroduce bleed.
    md = ("## Alpha Section\n\n| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n\n"
          "## Beta Section\n\nBeta body.\n")

    async def fake_convert(pdf_bytes):
        return ConvertedDoc(markdown=md, headings=[], page_texts=[md, ""],
                            table_pages=set(), images=[], engine="docling")

    monkeypatch.setattr(pi, "convert_pdf", fake_convert)
    monkeypatch.setattr(pi.settings, "pdf_vlm_escalation_enabled", True)
    monkeypatch.setattr(pi.settings, "pdf_vlm_max_pages_per_run", 30)

    calls = []

    async def fake_escalate(pdf_bytes, segment):
        calls.append(segment.title)
        return "REPLACED"

    monkeypatch.setattr(pi, "escalate_segment", fake_escalate)

    segs = await pi.build_segments(_outline_pdf())
    assert calls == []  # shared page → no escalation
    alpha = next(s for s in segs if s.title == "Alpha Section")
    assert "| 1 | 2 | 3 |" in alpha.markdown  # kept standard output
