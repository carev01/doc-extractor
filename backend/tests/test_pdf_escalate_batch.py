"""escalate_low_confidence_pages: page-level VLM escalation before the split."""
import os, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app.services.pdf_escalate as esc
from app.services.pdf_convert import ConvertedDoc, rebuild_from_pages
from app.services.pdf_escalate import pages_in_ranges

_RAGGED = "| a | b |\n| --- | --- |\n| 1 | 2 | 3 |"


def _conv(pages, page_texts, table_pages=None):
    md, starts = rebuild_from_pages(pages)
    return ConvertedDoc(markdown=md, headings=[], page_texts=page_texts,
                        table_pages=table_pages or set(), images=[],
                        engine="docling", page_line_starts=starts)


@pytest.mark.asyncio
async def test_escalates_only_flagged_pages_and_splices(monkeypatch):
    pages = ["## Intro\n\nclean text here", _RAGGED, "## Outro\n\nmore clean text"]
    conv = _conv(pages, page_texts=["x" * 50, "x" * 50, "x" * 50])
    monkeypatch.setattr(esc.settings, "pdf_vlm_escalation_enabled", True)
    monkeypatch.setattr(esc.settings, "pdf_vlm_max_pages_pct", 100.0)
    calls = []
    async def fake_page(pdf, p0):
        calls.append(p0)
        return "## Fixed\n\n| a | b |\n| --- | --- |\n| 1 | 2 |", []
    monkeypatch.setattr(esc, "escalate_page", fake_page)

    failed = await esc.escalate_low_confidence_pages(b"%PDF", conv)
    assert calls == [1]                       # only the ragged page
    assert failed == []
    assert "## Fixed" in conv.markdown        # spliced in
    assert "1 | 2 | 3" not in conv.markdown   # ragged content replaced
    assert "## Intro" in conv.markdown and "## Outro" in conv.markdown  # others intact


@pytest.mark.asyncio
async def test_only_restricts_to_target_pages(monkeypatch):
    # Retry targeting: even though all pages are flagged, only the requested page runs.
    pages = [_RAGGED, _RAGGED, _RAGGED]
    conv = _conv(pages, page_texts=["x" * 50] * 3)
    monkeypatch.setattr(esc.settings, "pdf_vlm_escalation_enabled", True)
    monkeypatch.setattr(esc.settings, "pdf_vlm_max_pages_pct", 100.0)
    calls = []
    async def ok(pdf, p0):
        calls.append(p0); return "## Fixed", []
    monkeypatch.setattr(esc, "escalate_page", ok)

    await esc.escalate_low_confidence_pages(b"x", conv, only={1})
    assert calls == [1]


@pytest.mark.asyncio
async def test_budget_caps_pages_and_defers_cosmetic(monkeypatch):
    # 12 ragged pages, 10% of 100 total pages → budget 10. The 2 over-budget pages
    # are cosmetic (ragged only), so they are NOT surfaced as pending.
    pages = [_RAGGED] * 12
    conv = _conv(pages, page_texts=["x" * 50] * 100)
    monkeypatch.setattr(esc.settings, "pdf_vlm_escalation_enabled", True)
    monkeypatch.setattr(esc.settings, "pdf_vlm_max_pages_pct", 10.0)
    calls = []
    async def ok(pdf, p0):
        calls.append(p0); return "## Fixed", []
    monkeypatch.setattr(esc, "escalate_page", ok)

    failed = await esc.escalate_low_confidence_pages(b"x", conv)
    assert len(calls) == 10
    assert failed == []


@pytest.mark.asyncio
async def test_over_budget_content_loss_is_surfaced(monkeypatch):
    # budget = 1 (1% of 2). page0 ragged (cosmetic) consumes it; page1 is empty
    # (content loss) and over budget → surfaced as pending, page0 is not.
    pages = [_RAGGED, ""]
    conv = _conv(pages, page_texts=["x" * 50, ""])
    monkeypatch.setattr(esc.settings, "pdf_vlm_escalation_enabled", True)
    monkeypatch.setattr(esc.settings, "pdf_vlm_max_pages_pct", 1.0)
    calls = []
    async def ok(pdf, p0):
        calls.append(p0); return "## Fixed", []
    monkeypatch.setattr(esc, "escalate_page", ok)

    failed = await esc.escalate_low_confidence_pages(b"x", conv)
    assert calls == [0]
    assert pages_in_ranges(failed) == {1}


@pytest.mark.asyncio
async def test_circuit_breaker_defers_remaining(monkeypatch):
    # Every page fails to escalate; after the breaker trips, remaining ragged
    # (cosmetic) pages are not surfaced — only the attempted-and-failed ones are.
    pages = [_RAGGED] * 10
    conv = _conv(pages, page_texts=["x" * 50] * 10)
    monkeypatch.setattr(esc.settings, "pdf_vlm_escalation_enabled", True)
    monkeypatch.setattr(esc.settings, "pdf_vlm_max_pages_pct", 100.0)
    monkeypatch.setattr(esc.settings, "pdf_vlm_max_consecutive_failures", 3)
    calls = []
    async def fail(pdf, p0):
        calls.append(p0); return None
    monkeypatch.setattr(esc, "escalate_page", fail)

    failed = await esc.escalate_low_confidence_pages(b"x", conv)
    assert len(calls) == 3                    # stopped after 3 consecutive failures
    assert pages_in_ranges(failed) == {0, 1, 2}
    assert conv.markdown.count(_RAGGED.splitlines()[-1]) == 10  # nothing changed


@pytest.mark.asyncio
async def test_disabled_is_noop(monkeypatch):
    conv = _conv([_RAGGED], page_texts=["x" * 50])
    monkeypatch.setattr(esc.settings, "pdf_vlm_escalation_enabled", False)
    called = []
    async def fake(pdf, p0):
        called.append(p0); return "x", []
    monkeypatch.setattr(esc, "escalate_page", fake)
    assert await esc.escalate_low_confidence_pages(b"x", conv) == []
    assert called == []


@pytest.mark.asyncio
async def test_no_page_offsets_is_noop(monkeypatch):
    # pymupdf fallback (no page_line_starts) → page-level work unavailable.
    conv = ConvertedDoc(markdown=_RAGGED, headings=[], page_texts=["x" * 50],
                        table_pages=set(), images=[], engine="pymupdf",
                        page_line_starts=[])
    monkeypatch.setattr(esc.settings, "pdf_vlm_escalation_enabled", True)
    called = []
    async def fake(pdf, p0):
        called.append(p0); return "x", []
    monkeypatch.setattr(esc, "escalate_page", fake)
    assert await esc.escalate_low_confidence_pages(b"x", conv) == []
    assert called == []
