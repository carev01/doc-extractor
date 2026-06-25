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


def test_detect_version_token_builds_template():
    base = "https://www.dell.com/manuals/pp-dm_20.1_cloud.htm"
    assert detect_version_token(base, "20.1") == \
        "https://www.dell.com/manuals/pp-dm_{version}_cloud.htm"


def test_detect_version_token_none_when_absent():
    assert detect_version_token("https://x/manuals/guide.htm", "20.1") is None
