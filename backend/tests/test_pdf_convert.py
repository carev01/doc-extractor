import base64
import os
import sys

import fitz
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.pdf_convert as pc
import app.services.docling_client as dc


def _pdf() -> bytes:
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((72, 72), "Alpha body content here.")
    return doc.tobytes()


@pytest.mark.asyncio
async def test_convert_pdf_parses_docling_response(monkeypatch):
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    data_uri = "data:image/png;base64," + base64.b64encode(png).decode()
    md = f"# Alpha\n\n![pic]({data_uri})\n"
    json_content = {
        "texts": [
            {"label": "section_header", "text": "Alpha", "level": 1,
             "prov": [{"page_no": 1}]},
            {"label": "page_footer", "text": "HYCU | 1", "prov": [{"page_no": 1}]},
        ],
        "tables": [{"prov": [{"page_no": 1}]}],
    }

    async def fake_convert(pdf_bytes, **kw):
        return {"md_content": md, "json_content": json_content}

    monkeypatch.setattr(pc.docling_client, "convert", fake_convert)
    monkeypatch.setattr(pc.settings, "pdf_converter", "docling")

    out = await pc.convert_pdf(_pdf())
    assert out.engine == "docling"
    assert [h.text for h in out.headings] == ["Alpha"]
    assert out.headings[0].level == 1 and out.headings[0].page0 == 0
    assert out.table_pages == {0}
    assert len(out.images) == 1 and out.images[0].filename.endswith(".png")
    assert out.images[0].filename in out.markdown
    assert "data:image/png" not in out.markdown


@pytest.mark.asyncio
async def test_convert_pdf_falls_back_to_pymupdf(monkeypatch):
    async def boom(pdf_bytes, **kw):
        raise dc.DoclingServeError("down")

    monkeypatch.setattr(pc.docling_client, "convert", boom)
    monkeypatch.setattr(pc.settings, "pdf_converter", "docling")

    out = await pc.convert_pdf(_pdf())
    assert out.engine == "pymupdf"
    assert "Alpha" in out.markdown
    assert len(out.page_texts) == 1


def test_content_address_data_uris():
    png = b"\x89PNG\r\n\x1a\n" + b"1" * 16
    uri = "data:image/png;base64," + base64.b64encode(png).decode()
    md = f"x ![cat]({uri}) y"
    new_md, images = pc._content_address_data_uris(md)
    assert len(images) == 1
    assert images[0].data == png
    assert images[0].filename in new_md
    assert "base64" not in new_md
