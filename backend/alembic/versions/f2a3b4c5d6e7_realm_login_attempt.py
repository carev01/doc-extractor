"""auth_realms.last_login_attempt_at

Stamped on every scripted-login attempt (success or Browserless failure), so
the once-per-JobRun login guard also holds during a Browserless outage, which
leaves a realm EXPIRED rather than LOGIN_FAILED.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
"""

import sqlalchemy as sa
from alembic import op

revision = "f2a3b4c5d6e7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "auth_realms",
        sa.Column("last_login_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("auth_realms", "last_login_attempt_at")
