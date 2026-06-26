import os
import sys

import fitz
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.pdf_import as pdf_import
from app.services.pdf_import import segment_pdf_async

pytestmark = pytest.mark.asyncio


def _pdf_plain_text() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Overview", fontsize=11)
    page.insert_text((72, 100), "Configuration", fontsize=11)
    return doc.tobytes()


async def test_llm_fallback_used_when_enabled(monkeypatch):
    monkeypatch.setattr(pdf_import.settings, "llm_fallback_enabled", True)

    async def fake_llm(text):
        return [{"title": "Overview", "level": 1},
                {"title": "Configuration", "level": 1}]

    monkeypatch.setattr(pdf_import, "_llm_segment_titles", fake_llm)
    segs = await segment_pdf_async(_pdf_plain_text())
    assert [s.title for s in segs] == ["Overview", "Configuration"]


async def test_outline_still_wins_without_calling_llm(monkeypatch):
    monkeypatch.setattr(pdf_import.settings, "llm_fallback_enabled", True)

    async def boom(text):
        raise AssertionError("LLM must not be called when an outline exists")

    monkeypatch.setattr(pdf_import, "_llm_segment_titles", boom)
    doc = fitz.open()
    doc.new_page(); doc.new_page()
    doc.set_toc([[1, "A", 1], [1, "B", 2]])
    segs = await segment_pdf_async(doc.tobytes())
    assert [s.title for s in segs] == ["A", "B"]


def _pdf_with_headings() -> bytes:
    """PDF with no outline but a large-font heading (24pt) followed by body (11pt)."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Introduction", fontsize=24)
    page.insert_text((72, 120), "This is body text for the introduction section.", fontsize=11)
    page2 = doc.new_page()
    page2.insert_text((72, 72), "Configuration", fontsize=24)
    page2.insert_text((72, 120), "Body text for the configuration section.", fontsize=11)
    return doc.tobytes()


async def test_heuristic_used_when_llm_returns_empty(monkeypatch):
    """When LLM is enabled but returns [], heuristic segments are used instead."""
    monkeypatch.setattr(pdf_import.settings, "llm_fallback_enabled", True)

    async def fake_llm_empty(text):
        return []

    monkeypatch.setattr(pdf_import, "_llm_segment_titles", fake_llm_empty)
    segs = await segment_pdf_async(_pdf_with_headings())
    assert len(segs) > 1
    titles = [s.title for s in segs]
    assert any("Introduction" in t or "Configuration" in t for t in titles)


async def test_llm_skipped_when_disabled(monkeypatch):
    """When llm_fallback_enabled is False, _llm_segment_titles is never called."""
    monkeypatch.setattr(pdf_import.settings, "llm_fallback_enabled", False)

    async def must_not_be_called(text):
        raise AssertionError("_llm_segment_titles must not be called when LLM is disabled")

    monkeypatch.setattr(pdf_import, "_llm_segment_titles", must_not_be_called)
    segs = await segment_pdf_async(_pdf_plain_text())
    assert len(segs) >= 1
