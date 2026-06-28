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
