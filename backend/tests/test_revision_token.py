"""Unit tests for the {rev} placeholder — the per-release token a vendor mints.

Motivating case, measured on the live portal: Cohesity NetBackup moved from
  /docs/netbackup/11.2/103228346-171368441-0/v95650213-171368441
to
  /docs/netbackup/11.2.0.1/103228346-173151032-0/v95650213-173151032
The publication (103228346) and topic (v95650213) ids held; the build id changed
and occurs TWICE. Templating only the version leaves the old build id in the key,
so every article of the source looks new at the next release.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio

from app.services.profiles.collapsible_sidebar import PROFILE
from app.services.versioning import (
    REVISION_PLACEHOLDER,
    derive_topic_key,
    extract_revision,
    like_pattern,
    resolve_template,
    templatize,
    upgrade_template,
)

TMPL = (
    "https://docs.cohesity.com/docs/netbackup/{version}"
    "/103228346-{rev}-0/v95650213-{rev}"
)
OLD = (
    "https://docs.cohesity.com/docs/netbackup/11.2"
    "/103228346-171368441-0/v95650213-171368441"
)
NEW = (
    "https://docs.cohesity.com/docs/netbackup/11.2.0.1"
    "/103228346-173151032-0/v95650213-173151032"
)
# A different topic in the same publication — shares the build id, not the topic id.
SIBLING_OLD = OLD.replace("v95650213", "v95379219")
SIBLING_NEW = NEW.replace("v95650213", "v95379219")


# ── The regression this exists to prevent ────────────────────────────────────

def test_topic_key_survives_a_release_that_changes_more_than_the_version():
    assert derive_topic_key(OLD, TMPL, "11.2") == derive_topic_key(NEW, TMPL, "11.2.0.1")


def test_the_stable_ids_are_preserved_in_the_key():
    # Publication and topic id must survive verbatim: they ARE the identity, and
    # a greedy token match that swallowed either would silently merge articles.
    key = derive_topic_key(NEW, TMPL, "11.2.0.1")
    assert "103228346" in key and "v95650213" in key
    assert "173151032" not in key and "171368441" not in key
    assert key.count(REVISION_PLACEHOLDER) == 2  # both occurrences, not just the first


def test_distinct_topics_keep_distinct_keys_across_the_bump():
    sibling = derive_topic_key(SIBLING_OLD, TMPL, "11.2")
    assert sibling == derive_topic_key(SIBLING_NEW, TMPL, "11.2.0.1")
    assert sibling != derive_topic_key(OLD, TMPL, "11.2")


def test_a_sibling_topic_resolves_its_own_revision():
    # The anchor is a PREFIX (…/<version>/103228346-), so extraction works for
    # every article of the publication — not only the one the template was cut
    # from. Without this, only one article per source would key stably.
    assert extract_revision(SIBLING_NEW, TMPL, "11.2.0.1") == "173151032"


# ── Refusals: a wrong token is worse than an untemplated one ─────────────────

def test_no_template_means_no_revision_templating():
    assert extract_revision(NEW, None, "11.2.0.1") is None
    plain = "https://docs.cohesity.com/docs/netbackup/{version}/103228346-173151032-0"
    assert extract_revision(NEW, plain, "11.2.0.1") is None


def test_a_url_outside_the_templates_publication_is_left_alone():
    # A sidebar can link another publication; its build id differs, so reading
    # this template's token off it would corrupt an unrelated URL.
    other = (
        "https://docs.cohesity.com/docs/netbackup/11.2.0.1"
        "/169433500-172152080-0/v131282878-172152080"
    )
    assert extract_revision(other, TMPL, "11.2.0.1") is None
    assert derive_topic_key(other, TMPL, "11.2.0.1") == other.replace(
        "11.2.0.1", "{version}", 1
    )


def test_a_mismatched_version_yields_no_revision():
    assert extract_revision(OLD, TMPL, "11.2.0.1") is None


def test_an_empty_token_is_refused_rather_than_returned():
    empty = "https://docs.cohesity.com/docs/netbackup/11.2.0.1/103228346--0/v9-x"
    assert extract_revision(empty, TMPL, "11.2.0.1") is None


def test_version_only_templates_are_untouched_by_the_revision_pass():
    # Every pre-existing source keeps exactly its old key.
    tmpl = "https://docs.example.com/UDP/Available/{version}/ENU/SolG/install.htm"
    url = "https://docs.example.com/UDP/Available/10.0/ENU/SolG/install.htm"
    assert derive_topic_key(url, tmpl, "10.0") == (
        "https://docs.example.com/UDP/Available/{version}/ENU/SolG/install.htm"
    )


# ── Resolving forward ────────────────────────────────────────────────────────

def test_resolve_template_fills_both_placeholders():
    assert resolve_template(TMPL, "11.2.0.1", "173151032") == NEW


def test_resolve_template_without_a_revision_leaves_the_placeholder():
    # Callers must supply one; silently emitting a half-resolved URL would look
    # like a working link.
    out = resolve_template(TMPL, "11.2.0.1")
    assert REVISION_PLACEHOLDER in out and "11.2.0.1" in out


# ── Healing a corpus stored before {rev} existed ─────────────────────────────

def test_like_pattern_spans_both_placeholders():
    assert like_pattern(derive_topic_key(NEW, TMPL, "11.2.0.1")) == (
        "https://docs.cohesity.com/docs/netbackup/%/103228346-%-0/v95650213-%"
    )


def test_like_pattern_escapes_metacharacters_in_the_literal_parts():
    key = "https://d/a_b/{version}/c%d-{rev}"
    pattern = like_pattern(key)
    assert r"a\_b" in pattern and r"c\%d" in pattern
    assert pattern.endswith("/%/c\\%d-%")


# ── The platform hooks ───────────────────────────────────────────────────────

def test_profile_templatizes_both_tokens():
    assert PROFILE.templatize_url(OLD, "11.2") == TMPL


def test_profile_refuses_urls_outside_its_grammar():
    assert PROFILE.templatize_url("https://example.com/docs/x", "11.2") is None
    assert PROFILE.templatize_url(OLD, "9.9") is None  # version not in this URL


def test_templatize_falls_back_to_version_only_without_a_profile():
    assert templatize(OLD, "11.2", None) == OLD.replace("11.2", "{version}", 1)


def test_templatize_survives_a_profile_hook_that_raises():
    class Boom:
        def templatize_url(self, url, version):
            raise RuntimeError("nope")

    assert templatize(OLD, "11.2", Boom()) == OLD.replace("11.2", "{version}", 1)


def test_resolve_revision_reads_the_build_id_for_its_own_publication():
    # The landing page lists several publications, each at its OWN build id — so
    # the lookup must be scoped by publication, not "first number that fits".
    landing = (
        '<a href="/docs/netbackup/11.2.0.1/169433500-172152080-0/v131282878-172152080">B</a>'
        '<a href="/docs/netbackup/11.2.0.1/103228346-173151032-0/v95650213-173151032">A</a>'
    )

    class FakeScraper:
        def __init__(self):
            self.fetched = []

        async def get_raw(self, url):
            self.fetched.append(url)
            return landing

    scraper = FakeScraper()
    rev = asyncio.run(PROFILE.resolve_revision(TMPL, "11.2.0.1", scraper))
    assert rev == "173151032"
    # One plain GET of the version landing page — no render, no Browserless.
    assert scraper.fetched == ["https://docs.cohesity.com/docs/netbackup/11.2.0.1"]


def test_resolve_revision_returns_none_when_the_fetch_fails():
    class Dead:
        async def get_raw(self, url):
            raise RuntimeError("unreachable")

    assert asyncio.run(PROFILE.resolve_revision(TMPL, "11.2.0.1", Dead())) is None


def test_resolve_revision_returns_none_when_the_publication_is_absent():
    class Empty:
        async def get_raw(self, url):
            return '<a href="/docs/netbackup/11.2.0.1/999-111-0/v1-111">other</a>'

    assert asyncio.run(PROFILE.resolve_revision(TMPL, "11.2.0.1", Empty())) is None


# ── Upgrading a template written before {rev} existed ────────────────────────
# Nothing else does this: the in-run auto-detection only fires when there is no
# template, and the UI only offers "Templatize" then too. An un-upgraded source
# silently re-keys its whole corpus at the vendor's next release.

VERSION_ONLY = OLD.replace("11.2/", "{version}/", 1)


def test_a_version_only_template_is_upgraded_to_carry_the_token():
    assert upgrade_template(VERSION_ONLY, OLD, "11.2", PROFILE) == TMPL


def test_an_already_upgraded_template_is_left_alone():
    assert upgrade_template(TMPL, OLD, "11.2", PROFILE) is None


def test_no_upgrade_without_a_profile_that_knows_the_grammar():
    assert upgrade_template(VERSION_ONLY, OLD, "11.2", None) is None

    class Bare:
        name = "generic"

    assert upgrade_template(VERSION_ONLY, OLD, "11.2", Bare()) is None


def test_upgrade_is_refused_when_the_round_trip_does_not_reproduce_base_url():
    # The gate that makes this safe to apply unreviewed: a candidate that merely
    # resembles the URL must not be adopted, because adopting it rewrites the
    # identity of every article in the source.
    class Sloppy:
        def templatize_url(self, url, version):
            # Drops the "-0" variant segment, so it resolves to a different URL.
            return (
                "https://docs.cohesity.com/docs/netbackup/{version}"
                "/103228346-{rev}/v95650213-{rev}"
            )

    assert upgrade_template(VERSION_ONLY, OLD, "11.2", Sloppy()) is None


def test_upgrade_survives_a_hook_that_raises():
    class Boom:
        def templatize_url(self, url, version):
            raise RuntimeError("nope")

    assert upgrade_template(VERSION_ONLY, OLD, "11.2", Boom()) is None


def test_a_source_bumped_before_the_upgrade_still_recovers_in_one_run():
    """Order-of-operations regression: if the version is bumped while the
    template is still version-only, base_url ends up at the NEW version with the
    OLD build id — a 404. The run must then upgrade the template first and
    resolve the token second, recovering without operator action."""
    stale = "https://docs.cohesity.com/docs/netbackup/11.2.0.1/103228346-171368441-0/v95650213-171368441"
    upgraded = upgrade_template(VERSION_ONLY, stale, "11.2.0.1", PROFILE)
    assert upgraded == TMPL, "the template upgrade must not depend on base_url being live"
    # …and only then does the token lookup replace the stale build id.
    assert resolve_template(upgraded, "11.2.0.1", "173151032") == NEW
