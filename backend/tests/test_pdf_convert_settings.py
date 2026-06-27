import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import Settings


def _settings(**env):
    # Required fields have no defaults; supply dummies so Settings() constructs.
    base = dict(
        database_url="postgresql+asyncpg://x/y",
        database_url_sync="postgresql+psycopg2://x/y",
        firecrawl_api_url="http://x",
    )
    base.update(env)
    return Settings(**base)


def test_pdf_converter_defaults():
    s = _settings()
    assert s.pdf_converter == "docling"
    assert s.pdf_vlm_escalation_enabled is True
    assert s.pdf_vlm_base_url == "https://openrouter.ai/api/v1/chat/completions"
    assert s.pdf_vlm_api_key == ""
    assert s.pdf_vlm_model == "qwen/qwen3-vl-32b-instruct"
    assert s.pdf_vlm_max_pages_per_run == 30
    assert s.pdf_vlm_dpi == 150


def test_pdf_settings_override_from_env_kwargs():
    s = _settings(pdf_converter="pymupdf", pdf_vlm_max_pages_per_run=5)
    assert s.pdf_converter == "pymupdf"
    assert s.pdf_vlm_max_pages_per_run == 5
