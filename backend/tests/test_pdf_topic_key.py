import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.versioning import derive_pdf_topic_key


def test_slug_from_path():
    assert derive_pdf_topic_key(["Chapter 1", "Installation"]) == "chapter-1/installation"


def test_collapses_whitespace_and_punctuation():
    assert derive_pdf_topic_key(["A &  B!!", "C/D"]) == "a-b/c-d"


def test_empty_path_is_stable():
    assert derive_pdf_topic_key([]) == "document"
