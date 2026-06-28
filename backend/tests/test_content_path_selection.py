import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.firecrawl import _select_content_path


def test_authed_raw_http_uses_raw_http():
    assert _select_content_path(True, "raw_http", None) == "raw_http"


def test_unauthed_raw_http_uses_raw_http():
    assert _select_content_path(False, "raw_http", None) == "raw_http"


def test_authed_non_raw_uses_browserless():
    assert _select_content_path(True, None, None) == "browserless"


def test_browserless_render_engine_uses_browserless():
    assert _select_content_path(False, None, "browserless") == "browserless"


def test_plain_source_uses_firecrawl():
    assert _select_content_path(False, None, None) == "firecrawl"
