"""The ``dell_manuals`` profile: resolving a Dell PDF's per-release id ({rev}).

Dell serves each manual as ``dl.dell.com/content/manual<id>-<slug>.pdf``. The
``manual<id>`` is the file's only identity (the slug is cosmetic — any slug, even
one naming the wrong release, serves the same bytes; a wrong id 404s), and Dell
mints a new id for every release of every guide, so it can't be derived from the
version. What *is* derivable is the guide's manual page,
``www.dell.com/support/manuals/en-us/<product>/<doc code>_<version>``, which links
the current PDF. Measured live 2026-11-05: all 45 Avamar 19.12 / NetWorker 19.14
PDFs matched their page's link exactly.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import types

import pytest

from app.services.profiles import dell_manuals
from app.services.profiles.dell_manuals import PROFILE
from app.services.versioning import (
    extract_revision,
    resolve_template,
    upgrade_template,
)

BASE = (
    "https://dl.dell.com/content/manual25955871-dell-avamar-19-12-"
    "administration-guide.pdf?language=en-us"
)
TMPL = (
    "https://dl.dell.com/content/{rev}-dell-avamar-{version_dashed}-"
    "administration-guide.pdf?language=en-us"
)
PAGE = "https://www.dell.com/support/manuals/en-us/avamar-server/avamar_administration_guide_{version}"

# What the 19.12 manual page carries (trimmed): the English PDF link, plus the
# Portuguese edition a geo-redirected page links when ?lang= is missing.
PAGE_HTML = """
<a href="https://dl.dell.com/content/manual38490749-dell-avamar-19-12-guia-de-administra&#xE7;&#xE3;o.pdf?language=pt-br">PDF</a>
<a class="pdf" href="https://dl.dell.com/content/manual25955871-dell-avamar-19-12-administration-guide.pdf?language=en-us">Print PDF</a>
"""


def _source(page=PAGE):
    return types.SimpleNamespace(
        name="Avamar Administration Guide",
        profile_config={"dell_manual_page": page} if page else None,
    )


def _resolve(monkeypatch, html, source=None, version="19.12"):
    seen = []

    async def fake_fetch(url):
        seen.append(url)
        if isinstance(html, Exception):
            raise html
        return html

    monkeypatch.setattr(dell_manuals, "_fetch_page", fake_fetch)
    rev = asyncio.run(PROFILE.resolve_revision(
        TMPL, version, scraper=None, source=source or _source()
    ))
    return rev, seen


def test_templatize_marks_the_id_and_the_dashed_version():
    assert PROFILE.templatize_url(BASE, "19.12") == TMPL


def test_template_round_trips_to_the_stored_url():
    # upgrade_template's gate: the template must reproduce base_url byte for byte.
    assert resolve_template(TMPL, "19.12", "manual25955871") == BASE
    assert extract_revision(BASE, TMPL, "19.12") == "manual25955871"


def test_templatize_refuses_a_url_without_the_version():
    assert PROFILE.templatize_url(BASE, "19.14") is None
    assert PROFILE.templatize_url("https://docs.example.com/19-12/a.pdf", "19.12") is None


def test_version_only_templates_are_not_upgraded_behind_the_operators_back():
    # upgrade_template needs a template carrying {version}*; the profile must
    # only ever produce one for a URL that matches its grammar.
    assert upgrade_template(TMPL, BASE, "19.12", PROFILE) is None


def test_resolve_reads_the_english_pdf_off_the_versioned_manual_page(monkeypatch):
    rev, seen = _resolve(monkeypatch, PAGE_HTML, version="19.13")
    assert rev == "manual25955871"
    assert seen == [
        "https://www.dell.com/support/manuals/en-us/avamar-server/"
        "avamar_administration_guide_19.13?lang=en-us"
    ]


def test_resolve_ignores_other_language_editions(monkeypatch):
    only_pt = PAGE_HTML.split("\n")[1]
    rev, _ = _resolve(monkeypatch, only_pt)
    assert rev is None


@pytest.mark.parametrize("html", [None, "", "<html>no pdf here</html>", RuntimeError("403")])
def test_resolve_returns_none_when_the_page_does_not_answer(monkeypatch, html):
    rev, _ = _resolve(monkeypatch, html)
    assert rev is None


def test_resolve_needs_the_sources_manual_page(monkeypatch):
    rev, seen = _resolve(monkeypatch, PAGE_HTML, source=_source(page=None))
    assert rev is None
    assert seen == []


def test_detect_never_claims_a_web_source():
    # Pinned per source only; it has no TOC/content logic of its own.
    assert PROFILE.detect("<html>dell.com</html>", "https://www.dell.com/x") is False
