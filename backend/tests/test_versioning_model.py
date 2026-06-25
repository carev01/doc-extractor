# backend/tests/test_versioning_model.py
import os, sys, uuid
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.models import Product, DocumentationSource, Article, ExtractionRun


def test_new_columns_exist_on_models():
    assert hasattr(Product, "version") and hasattr(Product, "previous_version")
    assert hasattr(DocumentationSource, "url_template")
    assert hasattr(ExtractionRun, "version")
    assert hasattr(Article, "topic_key")
