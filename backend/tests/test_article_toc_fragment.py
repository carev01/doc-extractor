import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.models.article import Article

def test_article_has_toc_fragment_column():
    col = Article.__table__.columns.get("toc_fragment")
    assert col is not None
    assert col.nullable is True
