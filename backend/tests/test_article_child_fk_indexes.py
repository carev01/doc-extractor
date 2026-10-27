"""article_versions.article_id and article_images.article_id must be indexed.

Both are ON DELETE CASCADE FKs to articles, so deleting an article makes Postgres
look up its versions and images — a sequential scan of the whole table per
deleted row without an index. Measured on a production copy (276 MB of versions):
the NetBackup split's bulk delete of ~14.5k articles ran >10 minutes and was
stopped; with the two indexes the whole split took 22 seconds. The same lookups
serve every per-article image and version read.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models import ArticleVersion
from app.models.image import ArticleImage


def _indexed(table, column):
    return any([c.name for c in idx.columns] == [column] for idx in table.indexes)


def test_article_versions_article_id_is_indexed():
    assert _indexed(ArticleVersion.__table__, "article_id")


def test_article_images_article_id_is_indexed():
    assert _indexed(ArticleImage.__table__, "article_id")
