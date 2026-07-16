"""Escalating a near-empty page BEFORE the split lets its outline sub-section
become its own article — the fix for a chapter (e.g. Cohesity "Cloud Platforms")
that the standard pipeline rendered as one empty stub instead of AWS/Azure/GCP.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.pdf_escalate as esc
from app.services.pdf_convert import ConvertedDoc, rebuild_from_pages, split_into_segments
from app.services.pdf_import import Segment


def _conv(pages, page_texts):
    md, starts = rebuild_from_pages(pages)
    return ConvertedDoc(markdown=md, headings=[], page_texts=page_texts,
                        table_pages=set(), images=[], engine="docling",
                        page_line_starts=starts)


# Outline (from the PDF bookmarks) has BOTH the chapter and its sub-section.
_OUTLINE = [
    Segment(title="Cloud Platforms", level=1, page_start=0, page_end=1, path=["Cloud Platforms"]),
    Segment(title="Azure", level=2, page_start=1, page_end=1, path=["Cloud Platforms", "Azure"]),
]


def test_azure_is_not_split_when_page_is_empty():
    # Page 1 (Azure) rendered empty by the standard pipeline → no heading to match,
    # so it folds into the chapter and no Azure article is created.
    conv = _conv(["# Cloud Platforms\n\nOverview of cloud platforms.", ""],
                 page_texts=["Overview of cloud platforms.", ""])
    titles = [s.title for s in split_into_segments(conv, _OUTLINE)]
    assert "Azure" not in titles


@pytest.mark.asyncio
async def test_escalation_recovers_azure_as_its_own_article(monkeypatch):
    conv = _conv(["# Cloud Platforms\n\nOverview of cloud platforms.", ""],
                 page_texts=["Overview of cloud platforms.", ""])
    monkeypatch.setattr(esc.settings, "pdf_vlm_escalation_enabled", True)
    monkeypatch.setattr(esc.settings, "pdf_vlm_max_pages_pct", 100.0)

    async def fake_page(pdf, p0):
        # The VLM reads the rendered page and emits the Azure heading + content.
        assert p0 == 1                         # only the empty page is escalated
        return "## Azure\n\nProtect Azure workloads with the connector.", []

    monkeypatch.setattr(esc, "escalate_page", fake_page)

    failed = await esc.escalate_low_confidence_pages(b"%PDF", conv)
    assert failed == []

    titles = [s.title for s in split_into_segments(conv, _OUTLINE)]
    assert "Azure" in titles                   # now its own article
    azure = next(s for s in split_into_segments(conv, _OUTLINE) if s.title == "Azure")
    assert "Protect Azure workloads" in azure.markdown
