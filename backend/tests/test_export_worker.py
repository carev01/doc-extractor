import os, sys, uuid
import pytest
from unittest.mock import patch
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.core.config import settings
from app.core.database import Base
from app.models import Vendor, Product, DocumentationSource, Article, ExportJob
from app.models.export_job import ExportStatus
from app.services.export_runner import run_export_job_sync
from app.services.exporter import export_engine

URL = settings.database_url_sync.rsplit("/", 1)[0] + "/docextractor_test"
eng = create_engine(URL); SyncS = sessionmaker(eng, class_=Session, expire_on_commit=False)


@pytest.fixture
def db():
    Base.metadata.drop_all(eng); Base.metadata.create_all(eng)
    s = SyncS(); yield s; s.close(); Base.metadata.drop_all(eng)


def test_run_export_job_completes(db):
    v = Vendor(name="EJ"); db.add(v); db.flush()
    s_prod = Product(vendor_id=v.id, name="P")
    db.add(s_prod)
    db.flush()
    s = DocumentationSource(product_id=s_prod.id, name="EJSrc", base_url="https://ej.com")
    db.add(s); db.flush()
    for i in range(3):
        db.add(Article(source_id=s.id, title=f"A{i}", source_url=f"https://ej.com/{i}",
                       content_markdown=f"# A{i}\n\nx", sort_order=i,
                       estimated_tokens=10, content_size_bytes=50))
    job = ExportJob(source_id=s.id, request={"source_id": str(s.id), "format": "pdf"},
                    status=ExportStatus.RUNNING)
    db.add(job); db.commit()
    jid = job.id

    run_export_job_sync(jid, session_factory=SyncS)

    db2 = SyncS()
    job = db2.execute(select(ExportJob).where(ExportJob.id == jid)).scalar_one()
    assert job.status == ExportStatus.COMPLETED
    assert job.export_id is not None
    export_dir = os.path.join(export_engine.export_dir, str(job.export_id))
    files = os.listdir(export_dir)
    # PDF export delivers the self-contained PDF, not a redundant zip wrapping it.
    assert any(f.endswith(".pdf") for f in files), files
    assert not any(f.endswith(".zip") for f in files), files
    db2.close()


def test_run_export_job_fails_on_generation_error(db):
    """run_export_job_sync must mark the job FAILED when export_engine.export_sync raises."""
    v = Vendor(name="EJFail"); db.add(v); db.flush()
    s_prod = Product(vendor_id=v.id, name="P")
    db.add(s_prod)
    db.flush()
    s = DocumentationSource(product_id=s_prod.id, name="EJFailSrc", base_url="https://ejfail.com")
    db.add(s); db.flush()
    job = ExportJob(source_id=s.id, request={"source_id": str(s.id), "format": "markdown"},
                    status=ExportStatus.RUNNING)
    db.add(job); db.commit()
    jid = job.id

    with patch("app.services.export_runner.export_engine.export_sync",
               side_effect=RuntimeError("simulated generation failure")):
        run_export_job_sync(jid, session_factory=SyncS)

    db2 = SyncS()
    job = db2.execute(select(ExportJob).where(ExportJob.id == jid)).scalar_one()
    assert job.status == ExportStatus.FAILED
    assert job.error_message  # non-empty
    db2.close()
