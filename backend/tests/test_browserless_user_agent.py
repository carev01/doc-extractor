"""Browserless must not navigate as HeadlessChrome.

Measured against docs.cohesity.com on 2026-09-16: Browserless' default UA
(`HeadlessChrome/151.0.0.0`) gets a 403 whose body is 49 bytes with no DOM, so
`waitForSelector("[data-slot='sidebar-inner']")` timed out 30s later, the TOC
collapsed to a synthetic 1-page "Index", and the collapse guard aborted the run
against a 14,808-article baseline. With a real Chrome UA the same URL returns
200, one sidebar and 1,297 expand triggers.

The navigation "succeeding" with an error page is what makes this worth pinning:
nothing upstream sees a failure, so the symptom surfaces as a selector timeout
that reads like a site redesign.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.services import browserless as bl
from app.services.browserless import (
    _FUNCTION_SIGNATURE,
    BrowserlessClient,
    BrowserlessError,
)

SCRIPTS = {
    name: value
    for name, value in vars(bl).items()
    if name.endswith("_CODE") and isinstance(value, str)
}


def test_there_are_scripts_to_check():
    # Guard against the discovery above silently matching nothing.
    assert len(SCRIPTS) >= 5, SCRIPTS.keys()


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_every_script_can_be_patched(name):
    """The injection is keyed on one exact signature; a script that drifted from
    it would be posted unpatched and fail only on sites that check the UA."""
    assert _FUNCTION_SIGNATURE in SCRIPTS[name]


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_the_user_agent_precedes_every_navigation(name):
    patched = BrowserlessClient(url="http://x", token="")._with_user_agent(SCRIPTS[name])
    ua_at = patched.index("setUserAgent")
    for goto in ("page.goto",):
        idx = patched.find(goto)
        if idx != -1:
            assert ua_at < idx, f"{name}: UA set after the first {goto}"


def test_injection_is_idempotent_per_call_and_leaves_the_source_alone():
    client = BrowserlessClient(url="http://x", token="")
    original = SCRIPTS["_COLLAPSIBLE_EXPAND_CODE"]
    once = client._with_user_agent(original)
    assert once.count("setUserAgent") == 1
    # The module constant must not be mutated — later calls would compound it.
    assert SCRIPTS["_COLLAPSIBLE_EXPAND_CODE"] == original
    assert client._with_user_agent(original).count("setUserAgent") == 1


def test_the_agent_string_is_embedded_as_valid_js():
    client = BrowserlessClient(url="http://x", token="")
    patched = client._with_user_agent(_FUNCTION_SIGNATURE + "\n}")
    # json.dumps, not bare quotes: a UA containing a quote would otherwise break
    # the script and come back as an opaque 400.
    assert json.dumps(client.user_agent) in patched


def test_a_quote_in_the_agent_cannot_break_the_script():
    client = BrowserlessClient(url="http://x", token="")
    client.user_agent = 'Mozilla/5.0 "quoted" \\ backslash'
    patched = client._with_user_agent(_FUNCTION_SIGNATURE + "\n}")
    assert '\\"quoted\\"' in patched


def test_an_unrecognised_signature_raises_rather_than_shipping_unpatched():
    client = BrowserlessClient(url="http://x", token="")
    with pytest.raises(BrowserlessError, match="unrecognised signature"):
        client._with_user_agent("export default async function (page) { }")


def test_an_empty_agent_disables_injection():
    client = BrowserlessClient(url="http://x", token="")
    client.user_agent = ""
    code = _FUNCTION_SIGNATURE + "\n}"
    assert client._with_user_agent(code) == code


def test_the_default_agent_does_not_advertise_headless():
    client = BrowserlessClient(url="http://x", token="")
    assert "headless" not in client.user_agent.lower(), (
        "the whole point: HeadlessChrome is what the edge blocks"
    )
    assert "Chrome/" in client.user_agent
