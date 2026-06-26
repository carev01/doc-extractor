import hashlib
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.pdf_import as pdf_import
from app.services.pdf_import import acquire_pdf, pdf_is_upload, pdf_path_for

pytestmark = pytest.mark.asyncio


def _src(base_url):
    import uuid
    return types.SimpleNamespace(id=uuid.uuid4(), base_url=base_url)


async def test_upload_origin_reads_file_and_hashes(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_import.settings, "pdf_dir", str(tmp_path))
    src = _src("file://x.pdf")
    data = b"%PDF-1.4 fake bytes"
    with open(pdf_path_for(src.id, str(tmp_path)), "wb") as fh:
        fh.write(data)
    blob, digest = await acquire_pdf(src)
    assert blob == data
    assert digest == hashlib.sha256(data).hexdigest()
    assert pdf_is_upload(src) is True


async def test_url_origin_downloads_and_hashes(monkeypatch):
    src = _src("https://example.com/doc.pdf")
    data = b"%PDF-1.4 url bytes"

    async def fake_fetch(url):
        assert url == src.base_url
        return data

    monkeypatch.setattr(pdf_import, "_fetch_url_bytes", fake_fetch)
    blob, digest = await acquire_pdf(src)
    assert blob == data
    assert digest == hashlib.sha256(data).hexdigest()
    assert pdf_is_upload(src) is False
