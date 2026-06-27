"""PDF conversion must run OFF the asyncio event loop.

Root cause of the "stuck large PDF" bug: the per-segment markdown conversion ran
synchronously on the event loop, starving the worker's heartbeat (and log-flush)
tasks. Once heartbeat_at went stale (>300s) the scheduler reaped the run, so a
large PDF (e.g. HYCU's 273-segment User Guide) could never finish.

These tests pin the fix: convert_segments_async offloads each render to a thread
(keeping the loop free) and reports per-segment progress.
"""

import asyncio
import os
import sys
import time

import fitz
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import pdf_import
from app.services.pdf_import import Segment

pytestmark = pytest.mark.asyncio


def _make_pdf(n_pages: int = 1) -> bytes:
    doc = fitz.open()
    for _ in range(n_pages):
        doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


async def test_convert_segments_async_reports_progress_in_order(monkeypatch):
    pdf = _make_pdf(3)
    segs = [Segment(title=f"S{i}", level=1, page_start=i, page_end=i, path=[f"S{i}"])
            for i in range(3)]
    monkeypatch.setattr(pdf_import, "_render_segment",
                        lambda doc, seg: (f"md-{seg.title}", []))

    seen: list[tuple[int, int]] = []

    async def progress(done: int, total: int) -> None:
        seen.append((done, total))

    rendered = await pdf_import.convert_segments_async(pdf, segs, progress)

    assert [md for md, _ in rendered] == ["md-S0", "md-S1", "md-S2"]
    assert seen == [(1, 3), (2, 3), (3, 3)]


async def test_convert_segments_async_does_not_block_event_loop(monkeypatch):
    """A concurrent coroutine must keep running while segments render — proving
    the CPU-bound work is off the loop. With a synchronous on-loop conversion
    this ticker would not advance until conversion finished."""
    pdf = _make_pdf(1)
    segs = [Segment(title=f"S{i}", level=1, page_start=0, page_end=0, path=[f"S{i}"])
            for i in range(3)]

    def slow_render(doc, seg):
        time.sleep(0.05)  # simulate blocking CPU work inside the thread
        return ("md", [])

    monkeypatch.setattr(pdf_import, "_render_segment", slow_render)

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    t = asyncio.create_task(ticker())
    rendered = await pdf_import.convert_segments_async(pdf, segs, None)
    t.cancel()

    assert len(rendered) == 3
    # ~0.15s of blocking renders / 0.005s ticks → the loop stayed responsive.
    assert ticks >= 5
