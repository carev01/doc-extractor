"""The bump pre-flight must plan against the template the RUN will use.

Regression, observed live: NetBackup sat on a version-only template (the upgrade
only fires at run start, and no run had happened since deploy). The pre-flight
read that stored template, saw no {rev}, reported a pure substitution — and
showed a green "ready" over
  .../11.2.0.1/103228346-171368441-0/v95650213-171368441
whose build id is a release out of date. That URL 404s, which is the exact
outcome the pre-flight exists to prevent.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import types

import app.routes.products as products

BASE = (
    "https://docs.cohesity.com/docs/netbackup/11.2"
    "/103228346-171368441-0/v95650213-171368441"
)
VERSION_ONLY = BASE.replace("11.2/", "{version}/", 1)
WITH_REV = (
    "https://docs.cohesity.com/docs/netbackup/{version}"
    "/103228346-{rev}-0/v95650213-{rev}"
)
LIVE = (
    "https://docs.cohesity.com/docs/netbackup/11.2.0.1"
    "/103228346-173151032-0/v95650213-173151032"
)


def _source(template=VERSION_ONLY, base_url=BASE, platform="collapsible_sidebar"):
    return types.SimpleNamespace(
        id=uuid.uuid4(), name="Cohesity NetBackup",
        url_template=template, base_url=base_url, platform=platform,
    )


def _plan(monkeypatch, source, target, current=..., revision="173151032"):
    """Plan one source. Passing no *current* exercises the signature the caller
    had before this fix, so the assertions below fail on the old code for the
    reason they describe rather than on a TypeError."""
    async def fake_resolve(self, template, version, scraper):
        return revision

    from app.services.profiles.collapsible_sidebar import PROFILE

    monkeypatch.setattr(
        PROFILE, "resolve_revision", types.MethodType(fake_resolve, PROFILE)
    )
    args = [source], target
    if current is not ...:
        args = [source], target, current
    return asyncio.run(products._plan_bump(*args))[0]


def test_the_preflight_never_greenlights_a_stale_build_id(monkeypatch):
    """The live symptom, pinned: a version-only source must not be reported as a
    plain substitution onto a URL carrying the previous release's build id."""
    entry = _plan(monkeypatch, _source(), "11.2.0.1")
    assert entry["status"] != "ok", "a {rev} platform is never a pure substitution"
    assert "171368441" not in entry["resolved_url"], (
        "the preview showed a URL a release out of date, which 404s"
    )
    assert entry["resolved_url"] == LIVE


def test_a_version_only_source_is_planned_against_its_upgraded_template(monkeypatch):
    entry = _plan(monkeypatch, _source(), "11.2.0.1", "11.2")
    assert entry["template_upgraded"] is True
    assert entry["url_template"] == WITH_REV
    assert entry["status"] == "resolved"
    assert entry["revision"] == "173151032"
    # The point of the whole exercise: the URL shown is the one that works.
    assert entry["resolved_url"] == LIVE
    assert "171368441" not in entry["resolved_url"]


def test_an_already_upgraded_source_is_not_reported_as_upgraded(monkeypatch):
    entry = _plan(monkeypatch, _source(template=WITH_REV), "11.2.0.1", "11.2")
    assert entry["template_upgraded"] is False
    assert entry["status"] == "resolved"
    assert entry["resolved_url"] == LIVE


def test_a_plain_version_only_source_still_reports_a_simple_substitution(monkeypatch):
    # No {rev} platform involved: behaviour must be exactly as before.
    src = _source(
        template="https://docs.example.com/{version}/guide.htm",
        base_url="https://docs.example.com/10.0/guide.htm",
        platform="generic",
    )
    entry = _plan(monkeypatch, src, "11.0", "10.0")
    assert entry["template_upgraded"] is False
    assert entry["status"] == "ok"
    assert entry["resolved_url"] == "https://docs.example.com/11.0/guide.htm"


def test_an_unresolvable_token_is_reported_not_papered_over(monkeypatch):
    entry = _plan(monkeypatch, _source(), "99.0", "11.2", revision=None)
    assert entry["status"] == "unresolved"
    # Falls back to the token the source is on now, so the URL stays well-formed.
    assert "171368441" in entry["resolved_url"]
