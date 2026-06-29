import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models.article import Article


def test_toc_entry_id_is_indexed():
    # The ON DELETE SET NULL back-reference is fired on every toc_entries delete
    # (TOC rebuild / re-run); the index avoids a full-table scan per deleted row.
    col = Article.__table__.columns["toc_entry_id"]
    assert col.index is True
    names = {ix.name for ix in Article.__table__.indexes}
    assert "ix_articles_toc_entry_id" in names
