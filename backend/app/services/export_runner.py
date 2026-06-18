"""Execute a claimed export job (synchronous generation) and record the result."""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.database import sync_session  # see note below
from app.models.export_job import ExportJob, ExportStatus
from app.services.exporter import export_engine

logger = logging.getLogger(__name__)


def run_export_job_sync(job_id: uuid.UUID, session_factory=None) -> None:
    """Generate the export for a RUNNING job and mark it completed/failed."""
    factory = session_factory or sync_session
    with factory() as db:
        job = db.execute(select(ExportJob).where(ExportJob.id == job_id)).scalar_one_or_none()
        if job is None:
            return
        req = dict(job.request)
        source_id = uuid.UUID(req["source_id"])
        try:
            result = export_engine.export_sync(
                db,
                source_id=source_id,
                article_ids=[uuid.UUID(x) for x in req["article_ids"]] if req.get("article_ids") else None,
                toc_entry_ids=[uuid.UUID(x) for x in req["toc_entry_ids"]] if req.get("toc_entry_ids") else None,
                topic_query=req.get("topic_query"),
                split_by=req.get("split_by"),
                max_articles_per_file=req.get("max_articles_per_file"),
                max_file_size_bytes=req.get("max_file_size_bytes"),
                max_tokens_per_file=req.get("max_tokens_per_file"),
                respect_chapters=req.get("respect_chapters", False),
                format=req.get("format", "markdown"),
            )
            job.export_id = result["export_id"]
            job.result = {k: v for k, v in result.items() if k != "export_id"}
            job.result["export_id"] = str(result["export_id"])
            job.result["source_id"] = str(result["source_id"])
            job.status = ExportStatus.COMPLETED
            job.completed_at = datetime.now(timezone.utc)
            db.commit()
        except Exception as exc:
            logger.exception("Export job %s failed", job_id)
            db.rollback()
            job = db.execute(select(ExportJob).where(ExportJob.id == job_id)).scalar_one()
            job.status = ExportStatus.FAILED
            job.error_message = str(exc)[:4096]
            job.completed_at = datetime.now(timezone.utc)
            db.commit()
