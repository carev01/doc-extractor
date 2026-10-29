"""job_exclusions: vendors a tier job excludes

Revision ID: j4d5e6f7a8b9
Revises: f2a3b4c5d6e7
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "j4d5e6f7a8b9"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_exclusions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False),
        sa.UniqueConstraint("job_id", "vendor_id", name="uq_job_exclusions_job_vendor"),
    )
    op.create_index("ix_job_exclusions_job_id", "job_exclusions", ["job_id"])
    op.create_index("ix_job_exclusions_vendor_id", "job_exclusions", ["vendor_id"])


def downgrade() -> None:
    op.drop_index("ix_job_exclusions_vendor_id", table_name="job_exclusions")
    op.drop_index("ix_job_exclusions_job_id", table_name="job_exclusions")
    op.drop_table("job_exclusions")
