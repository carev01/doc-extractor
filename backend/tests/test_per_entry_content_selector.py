"""The browserless content path resolves a content selector per TOC entry.

Most browserless profiles use one selector for the whole run (the profile's
``browserless_content_spec``). But a single page can hold several documents in
different sections (e.g. a changelog page with a Platform feed and a PMC feed),
so an individual entry may carry its own ``content_selector`` that overrides the
run default. This lets two articles be extracted from one URL by selecting
different sections.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.firecrawl import _content_selector_for


def test_entry_selector_overrides_run_default():
    entry = {"content_selector": "#updates"}
    spec = {"selector": "#divTopicContent"}
    assert _content_selector_for(entry, spec) == "#updates"


def test_falls_back_to_run_default_when_entry_has_none():
    entry = {"title": "x", "url": "u"}
    spec = {"selector": "#divTopicContent"}
    assert _content_selector_for(entry, spec) == "#divTopicContent"


def test_none_when_neither_present():
    assert _content_selector_for({"url": "u"}, None) is None
    assert _content_selector_for({"url": "u"}, {}) is None
