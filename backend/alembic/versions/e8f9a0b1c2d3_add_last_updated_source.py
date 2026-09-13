"""Add articles.last_updated_source (provenance for the vendor's revision date)

last_updated_at is the vendor's own claim about when a page changed. Which tier
that claim came from justifies different downstream actions — a consumer that
expires facts on a date must be able to tell a vendor-declared date from an
inferred one — so the provenance is recorded at write time rather than
reconstructed later.

Every existing non-null value (1,732 articles across Gearset, Druva, Trilio,
GRAX and Flosum) was produced by the one extractor that existed: a
<time datetime> element in the article body. That is `page_markup` — structured
markup the vendor emitted, distinct from a `page_text` tier (a date scraped out
of visible prose), which is weaker and which we do not implement.

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
"""
from alembic import op
import sqlalchemy as sa

revision = "e8f9a0b1c2d3"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "articles",
        sa.Column("last_updated_source", sa.String(length=24), nullable=True),
    )
    # Label what is already there. Left NULL wherever last_updated_at is NULL —
    # the pair is meaningless apart, and a source without a date would be noise.
    op.execute(
        """
        UPDATE articles
           SET last_updated_source = 'page_markup'
         WHERE last_updated_at IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_column("articles", "last_updated_source")
