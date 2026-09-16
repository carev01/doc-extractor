"""Group export jobs into a batch (product-level export).

A product-level export fans out into one ordinary per-source ExportJob each,
sharing a ``batch_id``. Fan-out rather than one product-scoped job is deliberate:
a 26-source export that fails on source 25 must not lose 1-24, each source keeps
the existing per-job retry/cancel machinery, and the single worker (which
prioritises extraction runs) can interleave between sources instead of being held
for the whole batch. It also leaves the export engine completely untouched.

Both columns are nullable — an ordinary single-source export has no batch.

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "f9a0b1c2d3e4"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "export_jobs", sa.Column("batch_id", UUID(as_uuid=True), nullable=True)
    )
    # Position within the batch: fixes the order sources are listed and zipped in,
    # so a batch reads the same way every time regardless of completion order.
    op.add_column(
        "export_jobs", sa.Column("batch_seq", sa.Integer(), nullable=True)
    )
    op.add_column(
        "export_jobs", sa.Column("batch_label", sa.String(length=512), nullable=True)
    )
    # Every batch read is "all jobs of this batch, in order".
    op.create_index(
        "ix_export_jobs_batch", "export_jobs", ["batch_id", "batch_seq"],
        postgresql_where=sa.text("batch_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_export_jobs_batch", table_name="export_jobs")
    op.drop_column("export_jobs", "batch_label")
    op.drop_column("export_jobs", "batch_seq")
    op.drop_column("export_jobs", "batch_id")
