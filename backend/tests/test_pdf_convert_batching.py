"""Batched docling conversion sends each batch as a small page-extracted PDF
(not the whole document + a page_range) and re-bases page numbers to absolute."""

import base64
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fitz
import pytest

import app.services.pdf_convert as pc


def _pdf(n: int) -> bytes:
    doc = fitz.open()
    for i in range(n):
        pg = doc.new_page()
        pg.insert_text((72, 72), f"Page {i + 1} content")
    data = doc.tobytes()
    doc.close()
    return data


def _page_count(b: bytes) -> int:
    d = fitz.open(stream=b, filetype="pdf")
    n = d.page_count
    d.close()
    return n


@pytest.mark.asyncio
async def test_batched_sends_small_extracts_and_offsets_pages(monkeypatch):
    monkeypatch.setattr(pc.settings, "pdf_convert_batch_pages", 3)
    received = []

    async def fake_convert(pdf_bytes, page_range=None, **kw):
        received.append({"pages": _page_count(pdf_bytes), "page_range": page_range})
        return {
            "md_content": "H",
            "json_content": {
                "texts": [{"label": "section_header", "text": "S", "level": 1,
                           "prov": [{"page_no": 1}]}],
                "tables": [{"prov": [{"page_no": 1}]}],
            },
        }

    monkeypatch.setattr(pc.docling_client, "convert_async", fake_convert)

    pdf = _pdf(7)  # 7 pages, batch size 3 → batches starting at pages 1, 4, 7
    merged = await pc._convert_docling_batched(pdf, 7)

    assert len(received) == 3
    for r in received:
        assert r["page_range"] is None          # no longer sends a page_range
        assert r["pages"] <= 3                   # only the batch's pages, not all 7

    # Each batch reported page_no=1; offsets make them absolute: 1, 4, 7.
    text_pages = sorted(t["prov"][0]["page_no"] for t in merged["json_content"]["texts"])
    table_pages = sorted(t["prov"][0]["page_no"] for t in merged["json_content"]["tables"])
    assert text_pages == [1, 4, 7]
    assert table_pages == [1, 4, 7]


def test_extract_page_range_yields_only_those_pages():
    pdf = _pdf(10)
    out = pc._extract_page_range(pdf, 4, 6)  # 1-based inclusive → 3 pages
    assert _page_count(out) == 3
    assert len(out) < len(pdf)


@pytest.mark.asyncio
async def test_batched_content_addresses_images_per_batch(monkeypatch):
    """Images are extracted per batch (base64 dropped from the merged markdown and
    deduped across batches) so the worker doesn't hold every image multiple times."""
    monkeypatch.setattr(pc.settings, "pdf_convert_batch_pages", 1)
    png = b"\x89PNG\r\n\x1a\n" + b"Z" * 24
    uri = "data:image/png;base64," + base64.b64encode(png).decode()

    async def fake_convert(pdf_bytes, **kw):
        # Same image embedded in every batch → must dedupe to one.
        return {"md_content": f"# H\n\n![pic]({uri})\n",
                "json_content": {"texts": [], "tables": []}}

    monkeypatch.setattr(pc.docling_client, "convert_async", fake_convert)

    pdf = _pdf(3)  # 3 pages, batch size 1 → 3 batches, same image each
    merged = await pc._convert_docling_batched(pdf, 3)
    assert "data:image" not in merged["md_content"]      # base64 stripped from md
    assert len(merged["images"]) == 1                    # deduped across batches
    assert merged["images"][0].data == png

    cd = pc._build_converted_doc(merged, pdf)             # uses pre-extracted images
    assert len(cd.images) == 1
    assert "data:image" not in cd.markdown
