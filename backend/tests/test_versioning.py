import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.versioning import (
    resolve_template, derive_topic_key, detect_version_token, VERSION_PLACEHOLDER,
)

ARC = "https://docs.example.com/UDP/Available/{version}/ENU/SolG/default.htm"


def test_resolve_template_substitutes_version():
    assert resolve_template(ARC, "10.0") == \
        "https://docs.example.com/UDP/Available/10.0/ENU/SolG/default.htm"


def test_derive_topic_key_swaps_version_for_placeholder():
    url = "https://docs.example.com/UDP/Available/10.0/ENU/SolG/install.htm"
    assert derive_topic_key(url, ARC, "10.0") == \
        "https://docs.example.com/UDP/Available/{version}/ENU/SolG/install.htm"


def test_derive_topic_key_is_stable_across_versions():
    u10 = "https://docs.example.com/UDP/Available/10.0/ENU/SolG/install.htm"
    u11 = "https://docs.example.com/UDP/Available/11.0/ENU/SolG/install.htm"
    assert derive_topic_key(u10, ARC, "10.0") == derive_topic_key(u11, ARC, "11.0")


def test_derive_topic_key_only_touches_prefix_occurrence():
    # The version string also appears in the topic slug; only the prefix one is swapped.
    tmpl = "https://docs.example.com/p/{version}/guide.htm"
    url = "https://docs.example.com/p/10.0/whats-new-in-10.0.htm"
    assert derive_topic_key(url, tmpl, "10.0") == \
        "https://docs.example.com/p/{version}/whats-new-in-10.0.htm"


def test_derive_topic_key_passthrough_when_not_templated():
    url = "https://docs.example.com/x/install.htm"
    assert derive_topic_key(url, None, None) == url


def test_derive_topic_key_none_url_returns_none():
    # url-less structural TOC node (Flare "book"/section header with no page):
    # must not crash even for a templated source with a version.
    assert derive_topic_key(None, ARC, "19.0") is None
    assert derive_topic_key("", ARC, "19.0") == ""


def test_derive_topic_key_templatizes_without_url_template():
    # CommCell incident: a run keyed pages with a NULL url_template. As long as
    # the version is known, the key must still be version-independent so it
    # matches the stored {version} keys instead of duplicating the source.
    url = "https://documentation.commvault.com/11.44/commcell-console/12345"
    assert derive_topic_key(url, None, "11.44") == \
        "https://documentation.commvault.com/{version}/commcell-console/12345"


def test_derive_topic_key_same_with_or_without_template():
    # The crucial invariant: presence/absence of url_template must NOT change the
    # key for the same (url, version) — otherwise re-extraction duplicates.
    tmpl = "https://documentation.commvault.com/{version}/commcell-console/index.html"
    url = "https://documentation.commvault.com/11.44/commcell-console/12345"
    assert derive_topic_key(url, tmpl, "11.44") == derive_topic_key(url, None, "11.44")


def test_derive_topic_key_passthrough_when_version_absent_from_url():
    # No template and the version doesn't appear in the URL → nothing to swap.
    url = "https://docs.example.com/x/install.htm"
    assert derive_topic_key(url, None, "10.0") == url


def test_detect_version_token_builds_template():
    base = "https://www.dell.com/manuals/pp-dm_20.1_cloud.htm"
    assert detect_version_token(base, "20.1") == \
        "https://www.dell.com/manuals/pp-dm_{version}_cloud.htm"


def test_detect_version_token_none_when_absent():
    assert detect_version_token("https://x/manuals/guide.htm", "20.1") is None
