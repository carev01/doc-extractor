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
    monkeypatch.setattr(esc.settings, "pdf_vlm_max_pages_pct", 100.0)  # budget ≥ all pages
    calls = []
    async def fake_one(pdf_bytes, segment):
        calls.append(segment.title)
        return "## Bad\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    monkeypatch.setattr(esc, "escalate_segment", fake_one)

    failed = await esc.escalate_segments(b"%PDF", segs, conv)
    assert calls == ["Bad"]
    assert "| 1 | 2 | 3 |" not in bad.markdown
    assert failed == []  # the escalation succeeded → nothing pending


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
    monkeypatch.setattr(esc.settings, "pdf_vlm_max_pages_pct", 100.0)  # budget ≥ all pages
    monkeypatch.setattr(esc.settings, "pdf_vlm_max_consecutive_failures", 3)
    calls = []
    async def fail_one(pdf_bytes, segment):
        calls.append(segment.title)
        return None
    monkeypatch.setattr(esc, "escalate_segment", fail_one)

    failed = await esc.escalate_segments(b"%PDF", segs, conv)
    # Stopped after the 3rd consecutive failure — not all 10 attempted.
    assert len(calls) == 3
    # Segments keep their original markdown (no successful re-conversion).
    assert all("| 1 | 2 | 3 |" in s.markdown for s in segs)
    # All 10 are reported pending: the 3 attempted-and-failed plus the 7 the
    # breaker skipped (the service is clearly down).
    assert failed == list(range(10))


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
    monkeypatch.setattr(esc.settings, "pdf_vlm_max_pages_pct", 50.0)  # 50% of 2 pages = 1
    monkeypatch.setattr(esc.settings, "pdf_vlm_max_consecutive_failures", 5)
    seq = [None, "## B\n\nfixed\n"]
    async def one(pdf_bytes, segment):
        return seq.pop(0)
    monkeypatch.setattr(esc, "escalate_segment", one)

    failed = await esc.escalate_segments(b"%PDF", segs, conv)
    # A failed (None) → budget untouched; B succeeded within the 1-page budget.
    assert segs[1].markdown == "## B\n\nfixed\n"
    assert failed == [0]  # only the first segment is pending


@pytest.mark.asyncio
async def test_escalate_budget_is_percentage_of_total_pages(monkeypatch):
    # 100-page doc, 10% budget → 10 pages. 12 single-page flagged segments all
    # succeed, so exactly 10 are escalated and the last 2 are budget-DEFERRED —
    # deferrals are not failures, so nothing is reported pending.
    segs = [
        _seg(f"S{i}", i, i, "## x\n\n| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n")
        for i in range(12)
    ]
    conv = ConvertedDoc(markdown="", headings=[], page_texts=["x" * 50] * 100,
                        table_pages=set(), images=[], engine="docling")
    monkeypatch.setattr(esc.settings, "pdf_vlm_escalation_enabled", True)
    monkeypatch.setattr(esc.settings, "pdf_vlm_max_pages_pct", 10.0)  # 10% of 100 = 10
    calls = []
    async def ok_one(pdf_bytes, segment):
        calls.append(segment.title)
        return "## x\n\nfixed\n"
    monkeypatch.setattr(esc, "escalate_segment", ok_one)

    failed = await esc.escalate_segments(b"%PDF", segs, conv)
    assert len(calls) == 10           # budget = 10% of 100 pages
    assert failed == []               # budget-deferred segments are not failures


@pytest.mark.asyncio
async def test_escalate_budget_rounds_up_min_one(monkeypatch):
    # A tiny doc (3 pages) at 10% rounds up to a 1-page budget, so a flagged
    # single-page segment still gets one escalation attempt.
    seg = _seg("Only", 0, 0, "## Only\n\n| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n")
    conv = ConvertedDoc(markdown="", headings=[], page_texts=["x" * 50] * 3,
                        table_pages=set(), images=[], engine="docling")
    monkeypatch.setattr(esc.settings, "pdf_vlm_escalation_enabled", True)
    monkeypatch.setattr(esc.settings, "pdf_vlm_max_pages_pct", 10.0)  # ceil(0.3) = 1
    calls = []
    async def ok_one(pdf_bytes, segment):
        calls.append(segment.title)
        return "## Only\n\nfixed\n"
    monkeypatch.setattr(esc, "escalate_segment", ok_one)

    await esc.escalate_segments(b"%PDF", [seg], conv)
    assert calls == ["Only"]


@pytest.mark.asyncio
async def test_over_budget_empty_pages_recorded_pending_ragged_deferred(monkeypatch):
    # budget = 1 page (1% of 100). Two 10-page exclusive segments, both over budget:
    #  - a near-empty chapter (empty_pages) is actual content loss → must be
    #    recorded pending so it's visible and a (budget-free) retry can recover it;
    #  - a ragged table on populated content is merely imperfect → silently deferred.
    empty = _seg("EmptyChap", 0, 9, "## EmptyChap\n")
    ragged = _seg("BigTable", 20, 29,
                  "## BigTable\n\n| a | b |\n| --- | --- |\n" + "| 1 | 2 | 3 |\n" * 40)
    segs = [empty, ragged]
    conv = ConvertedDoc(markdown="", headings=[], page_texts=[""] * 100,
                        table_pages=set(), images=[], engine="docling")
    monkeypatch.setattr(esc.settings, "pdf_vlm_escalation_enabled", True)
    monkeypatch.setattr(esc.settings, "pdf_vlm_max_pages_pct", 1.0)  # budget = 1 page
    called = []
    async def fake_one(pdf_bytes, segment):
        called.append(segment.title); return "x"
    monkeypatch.setattr(esc, "escalate_segment", fake_one)

    failed = await esc.escalate_segments(b"%PDF", segs, conv)
    assert called == []      # both over budget → nothing escalated this run
    assert failed == [0]     # only the near-empty chapter is pending; ragged deferred


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
