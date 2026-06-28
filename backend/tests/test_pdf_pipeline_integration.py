# backend/tests/test_pdf_pipeline_integration.py
import os, sys
import fitz, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app.services.pdf_import as pi
from app.services.pdf_convert import ConvertedDoc


def _outline_pdf():
    d = fitz.open()
    for _ in range(2): d.new_page()
    d.set_toc([[1, "Alpha Section", 1], [1, "Beta Section", 1]])
    return d.tobytes()


@pytest.mark.asyncio
async def test_process_segments_splits_and_calls_on_poll(monkeypatch):
    md = "## Alpha Section\n\nAlpha body.\n\n## Beta Section\n\nBeta body.\n"
    async def fake_convert(pdf_bytes, on_poll=None):
        if on_poll: await on_poll({"task_status": "started", "task_position": 0})
        return ConvertedDoc(markdown=md, headings=[], page_texts=[md, ""],
                            table_pages=set(), images=[], engine="docling")
    monkeypatch.setattr(pi, "convert_pdf", fake_convert)
    monkeypatch.setattr(pi.settings, "pdf_vlm_escalation_enabled", False)
    polls = []
    async def on_poll(s): polls.append(s["task_status"])
    outline = pi._outline_for(_outline_pdf())
    segs, conv = await pi.process_segments(_outline_pdf(), outline, on_poll=on_poll)
    assert [s.title for s in segs] == ["Alpha Section", "Beta Section"]
    assert "Beta" not in segs[0].markdown
    assert polls == ["started"]
