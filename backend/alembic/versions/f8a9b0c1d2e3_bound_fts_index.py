"""bound the FTS GIN index input so oversized pages can be stored

The full-text index expression ``to_tsvector('english', title || content)`` is
evaluated on every insert. Postgres rejects a to_tsvector input over 1 MB
("string is too long for tsvector … max 1048575 bytes"), so an oversized page
(e.g. a 5.8 MB document360 article) failed its insert and was dropped entirely —
silent data loss. Bound the input with ``left(…, 262143)`` (262143 * 4 max
UTF-8 bytes/char < 1048575, safe for any encoding). The article's full content
is unaffected; only FTS index coverage is capped to ~the first 262k characters.

Must stay byte-identical to exporter._TSV so the planner uses this index.

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-07-15 17:20:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'f8a9b0c1d2e3'
down_revision: Union[str, Sequence[str], None] = 'e7f8a9b0c1d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# New (bounded) and old (unbounded) index expressions.
_TSV_BOUNDED = (
    "to_tsvector('english', "
    "left(coalesce(title,'') || ' ' || coalesce(content_markdown,''), 262143))"
)
_TSV_UNBOUNDED = (
    "to_tsvector('english', coalesce(title,'') || ' ' || coalesce(content_markdown,''))"
)


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_articles_fts")
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_articles_fts ON articles USING GIN ({_TSV_BOUNDED})"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_articles_fts")
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_articles_fts ON articles USING GIN ({_TSV_UNBOUNDED})"
    )
