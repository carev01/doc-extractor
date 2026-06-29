import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models.article import Article


def test_source_url_is_indexed():
    # The post-scrape TOC rebuild re-links every article by source_url; the index
    # avoids a full-table scan per re-link / lookup.
    col = Article.__table__.columns["source_url"]
    assert col.index is True
    names = {ix.name for ix in Article.__table__.indexes}
    assert "ix_articles_source_url" in names
