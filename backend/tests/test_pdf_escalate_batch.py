import os, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app.services.pdf_escalate as esc
from app.services.pdf_convert import ConvertedDoc, RenderedSegment


def _seg(title, p0, p1, md):
    return RenderedSegment(title=title, level=1, path=[title], page_start=p0,
                           page_end=p1, markdown=md, images=[])


@pytest.mark.asyncio
async def test_escalate_segments_only_exclusive_flagged(monkeypatch):
    # 'Bad' owns page 0 and is flagged (ragged table) → escalated.
    # 'Shared'/'Other' both on page 1 → flagged but shared → skipped.
    bad = _seg("Bad", 0, 0, "## Bad\n\n| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n")
    shared = _seg("Shared", 1, 1, "## Shared\n\n| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n")
    other = _seg("Other", 1, 1, "## Other\n\nplain\n")
    segs = [bad, shared, other]
    conv = ConvertedDoc(markdown="", headings=[], page_texts=["x"*50, "y"*50],
                        table_pages=set(), images=[], engine="docling")
    monkeypatch.setattr(esc.settings, "pdf_vlm_escalation_enabled", True)
    monkeypatch.setattr(esc.settings, "pdf_vlm_max_pages_per_run", 30)
    calls = []
    async def fake_one(pdf_bytes, segment):
        calls.append(segment.title)
        return "## Bad\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    monkeypatch.setattr(esc, "escalate_segment", fake_one)

    await esc.escalate_segments(b"%PDF", segs, conv)
    assert calls == ["Bad"]
    assert "| 1 | 2 | 3 |" not in bad.markdown


@pytest.mark.asyncio
async def test_escalate_segments_circuit_breaks_on_consecutive_failures(monkeypatch):
    # Every flagged segment fails (escalate_segment returns None) → after the
    # configured threshold, stop attempting the rest instead of hammering the
    # service. Each is a ragged-table single-page exclusive segment.
    segs = [
        _seg(f"S{i}", i, i, "## x\n\n| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n")
        for i in range(10)
    ]
    conv = ConvertedDoc(markdown="", headings=[], page_texts=["x" * 50] * 10,
                        table_pages=set(), images=[], engine="docling")
    monkeypatch.setattr(esc.settings, "pdf_vlm_escalation_enabled", True)
    monkeypatch.setattr(esc.settings, "pdf_vlm_max_pages_per_run", 30)
    monkeypatch.setattr(esc.settings, "pdf_vlm_max_consecutive_failures", 3)
    calls = []
    async def fail_one(pdf_bytes, segment):
        calls.append(segment.title)
        return None
    monkeypatch.setattr(esc, "escalate_segment", fail_one)

    await esc.escalate_segments(b"%PDF", segs, conv)
    # Stopped after the 3rd consecutive failure — not all 10 attempted.
    assert len(calls) == 3
    # Segments keep their original markdown (no successful re-conversion).
    assert all("| 1 | 2 | 3 |" in s.markdown for s in segs)


@pytest.mark.asyncio
async def test_escalate_segments_failure_does_not_consume_budget(monkeypatch):
    # A failed attempt converts no pages, so it must not eat the page budget —
    # a later success can still run. First call fails, second succeeds.
    segs = [
        _seg("A", 0, 0, "## A\n\n| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n"),
        _seg("B", 1, 1, "## B\n\n| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n"),
    ]
    conv = ConvertedDoc(markdown="", headings=[], page_texts=["x" * 50] * 2,
                        table_pages=set(), images=[], engine="docling")
    monkeypatch.setattr(esc.settings, "pdf_vlm_escalation_enabled", True)
    monkeypatch.setattr(esc.settings, "pdf_vlm_max_pages_per_run", 1)  # budget = 1 page
    monkeypatch.setattr(esc.settings, "pdf_vlm_max_consecutive_failures", 5)
    seq = [None, "## B\n\nfixed\n"]
    async def one(pdf_bytes, segment):
        return seq.pop(0)
    monkeypatch.setattr(esc, "escalate_segment", one)

    await esc.escalate_segments(b"%PDF", segs, conv)
    # A failed (None) → budget untouched; B succeeded within the 1-page budget.
    assert segs[1].markdown == "## B\n\nfixed\n"


@pytest.mark.asyncio
async def test_escalate_segments_disabled_noop(monkeypatch):
    seg = _seg("Bad", 0, 0, "## Bad\n\n| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n")
    conv = ConvertedDoc(markdown="", headings=[], page_texts=["x"*50],
                        table_pages=set(), images=[], engine="docling")
    monkeypatch.setattr(esc.settings, "pdf_vlm_escalation_enabled", False)
    called = []
    async def fake_one(b, s): called.append(s.title); return "x"
    monkeypatch.setattr(esc, "escalate_segment", fake_one)
    await esc.escalate_segments(b"%PDF", [seg], conv)
    assert called == []
