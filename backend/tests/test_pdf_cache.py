"""pdf_cache: converted-doc round-trip (markdown, offsets, image bytes)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.services import pdf_cache
from app.services.pdf_convert import ConvertedDoc, RenderedImage


def _conv():
    return ConvertedDoc(
        markdown="# Doc\n\n![f](abcd1234.png)\n\nbody",
        headings=[],
        page_texts=["ignored - re-derived on load"],
        table_pages={0, 3},
        images=[RenderedImage(filename="abcd1234.png", data=b"PNGDATA", alt="f")],
        engine="docling",
        page_line_starts=[0, 2, 5],
    )


def test_save_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "pdf_cache_dir", str(tmp_path))
    conv = _conv()
    pdf_cache.save("hash1", conv)

    # page_texts is re-derived by the caller, not cached — pass fresh ones on load.
    fresh_texts = ["page 0 text", "page 1 text"]
    loaded = pdf_cache.load("hash1", fresh_texts)
    assert loaded is not None
    assert loaded.markdown == conv.markdown
    assert loaded.page_line_starts == conv.page_line_starts
    assert loaded.table_pages == conv.table_pages
    assert loaded.page_texts == fresh_texts            # caller-supplied
    assert len(loaded.images) == 1
    assert loaded.images[0].filename == "abcd1234.png"
    assert loaded.images[0].data == b"PNGDATA"         # bytes restored from blob store
    assert loaded.images[0].alt == "f"


def test_load_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "pdf_cache_dir", str(tmp_path))
    assert pdf_cache.load("nope", []) is None


def test_delete_removes_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "pdf_cache_dir", str(tmp_path))
    pdf_cache.save("h", _conv())
    assert pdf_cache.load("h", []) is not None
    pdf_cache.delete("h")
    assert pdf_cache.load("h", []) is None


def test_pdf_bytes_round_trip(tmp_path, monkeypatch):
    # The raw PDF is cached so an escalate-retry re-extracts pages without a
    # re-download (hence without a fresh auth session).
    monkeypatch.setattr(settings, "pdf_cache_dir", str(tmp_path))
    assert pdf_cache.has_pdf("h") is False
    assert pdf_cache.load_pdf("h") is None
    pdf_cache.save_pdf("h", b"%PDF-1.7 body")
    assert pdf_cache.has_pdf("h") is True
    assert pdf_cache.load_pdf("h") == b"%PDF-1.7 body"
    pdf_cache.delete_pdf("h")
    assert pdf_cache.has_pdf("h") is False
    assert pdf_cache.load_pdf("h") is None


def test_pdf_bytes_helpers_tolerate_empty_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "pdf_cache_dir", str(tmp_path))
    assert pdf_cache.has_pdf("") is False
    assert pdf_cache.load_pdf("") is None


def test_load_tolerates_missing_blob(tmp_path, monkeypatch):
    # If an image blob is gone, the image is dropped rather than failing the load.
    monkeypatch.setattr(settings, "pdf_cache_dir", str(tmp_path))
    pdf_cache.save("h", _conv())
    os.remove(os.path.join(str(tmp_path), "blobs", "abcd1234.png"))
    loaded = pdf_cache.load("h", [])
    assert loaded is not None
    assert loaded.images == []
