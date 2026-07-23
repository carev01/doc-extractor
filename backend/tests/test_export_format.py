"""Export output-format tests for ExportEngine._generate_export.

Pure (no DB): fake Article rows + a mocked PDF renderer, so we can assert what
files each format produces. A PDF is self-contained (images embedded), so a PDF
export must NOT also emit a redundant zip; markdown still bundles .md + images.
"""

import io
import os
import types
import uuid
from datetime import datetime, timezone

from pypdf import PdfWriter

import app.services.exporter as exporter_mod
from app.services.exporter import export_engine


def _minimal_pdf_bytes() -> bytes:
    """A real, valid 1-page PDF so pypdf can merge it during the PDF export path."""
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _fake_article(title: str, body: str):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        title=title,
        source_url=f"https://docs.example.com/{title.lower()}",
        last_updated_at=None,
        extracted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        content_markdown=body,
        estimated_tokens=max(1, len(body) // 4),
        images=[],
    )


def test_pdf_export_does_not_create_zip(monkeypatch, tmp_path):
    """A PDF export delivers the PDF only — no redundant zip wrapping the PDF."""
    pdf_bytes = _minimal_pdf_bytes()
    monkeypatch.setattr(
        exporter_mod, "render_markdown_to_pdf",
        lambda md, base_url=None: pdf_bytes,
    )
    monkeypatch.setattr(export_engine, "export_dir", str(tmp_path))
    group = [_fake_article("Athena", "athena body"), _fake_article("Redshift", "redshift body")]

    result = export_engine._generate_export(
        [group], "Satori", uuid.uuid4(), "pdf", lambda ids: group
    )

    subdir = os.path.join(str(tmp_path), str(result["export_id"]))
    files = os.listdir(subdir)
    assert any(f.endswith(".pdf") for f in files), files
    assert not any(f.endswith(".zip") for f in files), f"PDF export must not emit a zip: {files}"
    assert result["zip_filename"] is None


def test_markdown_export_still_zips(monkeypatch, tmp_path):
    """Markdown exports still bundle into a zip (the .md + loose image files)."""
    monkeypatch.setattr(export_engine, "export_dir", str(tmp_path))
    group = [_fake_article("Athena", "athena body")]

    result = export_engine._generate_export(
        [group], "Satori", uuid.uuid4(), "markdown", lambda ids: group
    )

    subdir = os.path.join(str(tmp_path), str(result["export_id"]))
    files = os.listdir(subdir)
    assert any(f.endswith(".md") for f in files), files
    assert any(f.endswith(".zip") for f in files), files
    assert result["zip_filename"] == "Satori.zip"


def test_markdown_export_zips_images_without_staging(monkeypatch, tmp_path):
    """Images are written into the zip DIRECTLY from media — the export dir must
    NOT hold an uncompressed images/ staging tree (the 2x footprint that
    overflowed the exports volume on image-heavy sources)."""
    import types
    import zipfile

    media = tmp_path / "media"
    exports = tmp_path / "exports"
    media.mkdir()
    exports.mkdir()
    monkeypatch.setattr(export_engine, "export_dir", str(exports))
    monkeypatch.setattr(export_engine, "media_root", str(media))

    art = _fake_article("Athena", "body with an image")
    art.images = [types.SimpleNamespace(local_filename="pic.png", local_path="/media/x/pic.png")]
    (media / str(art.id)).mkdir()
    (media / str(art.id) / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)

    result = export_engine._generate_export(
        [[art]], "Satori", uuid.uuid4(), "markdown", lambda ids: [art]
    )
    subdir = exports / str(result["export_id"])

    with zipfile.ZipFile(subdir / "Satori.zip") as zf:
        names = zf.namelist()
    assert f"images/{art.id}/pic.png" in names       # image bundled into the zip
    assert not (subdir / "images").exists()          # but NOT staged uncompressed on disk
