"""Add article change timestamps (content_changed_at / basis / source_changed_at)

Adds the "when did the content change" axis a temporal consumer needs, as
distinct from created_at/extracted_at which only say when WE crawled.

The backfill assigns every existing article a value in one of three tiers, and
records which tier it came from so a consumer can tell an exact value from a
bound:

  exact       the last add/update outbox row's timestamp. The outbox records
              every content mutation including image-caption enrichment, so this
              is the real "served markdown became current" instant. Available
              from 2026-07-11, when the outbox shipped.

  lower_bound no outbox row, but archived versions exist. Uses the newest
              version's timestamp — the moment that snapshot was superseded,
              i.e. the last RAW change. May be early: enrichment before the
              outbox shipped changed the served text without archiving a
              version, so it is undated.

  first_seen  neither. The article has never been observed changing, so its
              first-seen date is used. Still stable across re-crawls, which is
              the property that stops a consumer's graph reordering on
              re-ingestion.

source_changed_at is the raw-scrape axis and comes from version history alone —
a version row is archived only when the raw scrape differed, so its presence is
by construction "the vendor changed the page".

Revision ID: d7e8f9a0b1c2
Revises: c4d5e6f7a8b9
"""
from alembic import op
import sqlalchemy as sa

revision = "d7e8f9a0b1c2"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "articles",
        sa.Column("content_changed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "articles",
        sa.Column("content_changed_basis", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "articles",
        sa.Column("source_changed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ── Tier 1: exact, from the change outbox ────────────────────────────────
    # Set-based against a grouped subquery rather than a correlated one: the
    # outbox is ~189k rows over ~126k articles and a per-row MAX would be a
    # sequential scan each time.
    op.execute(
        """
        UPDATE articles a
           SET content_changed_at = c.last_change,
               content_changed_basis = 'exact'
          FROM (
                SELECT article_id, MAX(created_at) AS last_change
                  FROM content_changes
                 WHERE change_type IN ('added', 'updated')
                   AND article_id IS NOT NULL
                 GROUP BY article_id
               ) c
         WHERE a.id = c.article_id
        """
    )

    # ── Tier 2: lower bound, from archived versions ──────────────────────────
    op.execute(
        """
        UPDATE articles a
           SET content_changed_at = v.last_version,
               content_changed_basis = 'lower_bound'
          FROM (
                SELECT article_id, MAX(extracted_at) AS last_version
                  FROM article_versions
                 GROUP BY article_id
               ) v
         WHERE a.id = v.article_id
           AND a.content_changed_at IS NULL
        """
    )

    # ── Tier 3: first seen ───────────────────────────────────────────────────
    op.execute(
        """
        UPDATE articles
           SET content_changed_at = created_at,
               content_changed_basis = 'first_seen'
         WHERE content_changed_at IS NULL
        """
    )

    # ── source_changed_at: raw changes only ──────────────────────────────────
    op.execute(
        """
        UPDATE articles a
           SET source_changed_at = v.last_version
          FROM (
                SELECT article_id, MAX(extracted_at) AS last_version
                  FROM article_versions
                 GROUP BY article_id
               ) v
         WHERE a.id = v.article_id
        """
    )
    # Never re-scraped with different bytes → the source content is as we first
    # captured it.
    op.execute(
        """
        UPDATE articles
           SET source_changed_at = created_at
         WHERE source_changed_at IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("articles", "source_changed_at")
    op.drop_column("articles", "content_changed_basis")
    op.drop_column("articles", "content_changed_at")
