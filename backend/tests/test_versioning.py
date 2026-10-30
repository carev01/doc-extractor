import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.versioning import (
    resolve_template, derive_topic_key, detect_version_token, VERSION_PLACEHOLDER,
    version_forms, like_pattern,
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


# ── a template that carries the version more than once ───────────────────────
# Atempo's GitBook publishes each language as a site variant whose path repeats
# the version: /lina-7.4/lina-7.4-en/… Anchoring only the first placeholder left
# "lina-7.4-en" literal in every key, so a bump to 7.5 would re-key the whole
# guide (retire + re-add every article — the CommCell duplication class).
ATEMPO = "https://documentation.atempo.com/lina-{version}/lina-{version}-en"


def test_derive_topic_key_replaces_every_template_placeholder():
    url = "https://documentation.atempo.com/lina-7.4/lina-7.4-en/administration-guide/about-lina"
    assert derive_topic_key(url, ATEMPO, "7.4") == \
        "https://documentation.atempo.com/lina-{version}/lina-{version}-en/administration-guide/about-lina"


def test_multi_placeholder_key_is_stable_across_versions():
    u74 = "https://documentation.atempo.com/lina-7.4/lina-7.4-en/release-notes"
    u75 = "https://documentation.atempo.com/lina-7.5/lina-7.5-en/release-notes"
    assert derive_topic_key(u74, ATEMPO, "7.4") == derive_topic_key(u75, ATEMPO, "7.5")


def test_multi_placeholder_leaves_the_version_in_a_slug_alone():
    url = "https://documentation.atempo.com/lina-7.4/lina-7.4-en/whats-new-in-7.4"
    assert derive_topic_key(url, ATEMPO, "7.4") == \
        "https://documentation.atempo.com/lina-{version}/lina-{version}-en/whats-new-in-7.4"


# ── Derived version forms ({version_compact}, {version_major}) ────────────────
# Arctera's AEM Guides portal spells one version three ways in a single URL: the
# folder (25-1), a compact slug (backup-exec-251-readme) and, for System
# Recovery, the bare major in the guide folder (arctera-system-recovery-24-readme).

BE_README = (
    "https://docs.backupexec.com/us/en/backupexec-userdocs/release-notes/all-platforms/"
    "backup-exec-readme/{version}/backup-exec-{version_compact}-readme.html"
)
SR_README = (
    "https://docs.backupexec.com/us/en/sr-userdocs/release-notes/all-platforms/"
    "arctera-system-recovery-{version_major}-readme/{version}/"
    "arcteratm-system-recovery-{version}-readme.html"
)
RTC = (
    "https://docs.backupexec.com/us/en/backupexec-userdocs/supplemental-content/all-platforms/"
    "sdr-recover-this-computer-wizard-help/{version}/recover-this-computer-wizard-help.html"
)


def test_resolve_template_substitutes_derived_forms():
    assert resolve_template(BE_README, "25-1").endswith(
        "/backup-exec-readme/25-1/backup-exec-251-readme.html")
    assert resolve_template(SR_README, "24-1").endswith(
        "/arctera-system-recovery-24-readme/24-1/arcteratm-system-recovery-24-1-readme.html")


def test_version_forms_strip_separators_and_take_the_major():
    assert version_forms("25-1") == {
        "{version}": "25-1", "{version_compact}": "251", "{version_major}": "25",
    }
    assert version_forms("11.2.0.1")["{version_compact}"] == "11201"
    assert version_forms("11.2.0.1")["{version_major}"] == "11"


def test_derived_placeholders_are_anchored_positionally():
    url = ("https://docs.backupexec.com/us/en/backupexec-userdocs/release-notes/all-platforms/"
           "backup-exec-readme/25-1/backup-exec-251-readme/about-this-readme.html")
    assert derive_topic_key(url, BE_README, "25-1") == (
        "https://docs.backupexec.com/us/en/backupexec-userdocs/release-notes/all-platforms/"
        "backup-exec-readme/{version}/backup-exec-{version_compact}-readme/about-this-readme.html")


def test_major_placeholder_key_is_stable_across_a_major_bump():
    base = "https://docs.backupexec.com/us/en/sr-userdocs/release-notes/all-platforms/"
    u24 = base + "arctera-system-recovery-24-readme/24-1/arcteratm-system-recovery-24-1-readme/install.html"
    u25 = base + "arctera-system-recovery-25-readme/25-1/arcteratm-system-recovery-25-1-readme/install.html"
    assert derive_topic_key(u24, SR_README, "24-1") == derive_topic_key(u25, SR_README, "25-1")


def test_a_url_off_the_second_block_keeps_the_leading_anchors():
    # Walk stops at the first literal that doesn't line up; what it anchored stays.
    url = ("https://docs.backupexec.com/us/en/backupexec-userdocs/release-notes/all-platforms/"
           "backup-exec-readme/25-1/legal-notice.html")
    assert derive_topic_key(url, BE_README, "25-1") == (
        "https://docs.backupexec.com/us/en/backupexec-userdocs/release-notes/all-platforms/"
        "backup-exec-readme/{version}/legal-notice.html")


def test_slug_versions_templates_the_version_inside_page_slugs():
    url = ("https://docs.backupexec.com/us/en/backupexec-userdocs/supplemental-content/all-platforms/"
           "sdr-recover-this-computer-wizard-help/25-1/recover-this-computer-wizard-help/"
           "sdr-recover-this-computer-wizard-help-v251/creating-a-simple-volume.html")
    key = derive_topic_key(url, RTC, "25-1", slug_versions=True)
    assert key.endswith("/{version}/recover-this-computer-wizard-help/"
                        "sdr-recover-this-computer-wizard-help-v{version_compact}/creating-a-simple-volume.html")
    nxt = url.replace("25-1", "25-2").replace("v251", "v252")
    assert derive_topic_key(nxt, RTC, "25-2", slug_versions=True) == key


def test_slug_versions_templates_the_verbatim_form_in_slugs_too():
    url = ("https://docs.backupexec.com/us/en/sr-userdocs/release-notes/all-platforms/"
           "arctera-system-recovery-24-readme/24-1/arcteratm-system-recovery-24-1-readme/"
           "what-s-new-in-arctera-system-recovery-24-1.html")
    assert derive_topic_key(url, SR_README, "24-1", slug_versions=True).endswith(
        "/arcteratm-system-recovery-{version}-readme/what-s-new-in-arctera-system-recovery-{version}.html")


def test_slug_versions_needs_a_digit_boundary():
    # 2510 / 1251 are other numbers, not the 251 build of 25-1.
    url = ("https://docs.backupexec.com/us/en/backupexec-userdocs/supplemental-content/all-platforms/"
           "sdr-recover-this-computer-wizard-help/25-1/recover-this-computer-wizard-help/"
           "error-2510-and-1251.html")
    assert derive_topic_key(url, RTC, "25-1", slug_versions=True).endswith("/error-2510-and-1251.html")


def test_slugs_are_left_alone_without_the_opt_in():
    url = ("https://docs.backupexec.com/us/en/backupexec-userdocs/supplemental-content/all-platforms/"
           "sdr-recover-this-computer-wizard-help/25-1/recover-this-computer-wizard-help/"
           "sdr-recover-this-computer-wizard-help-v251/creating-a-simple-volume.html")
    assert derive_topic_key(url, RTC, "25-1").endswith("-v251/creating-a-simple-volume.html")


def test_like_pattern_wildcards_derived_placeholders():
    assert like_pattern("https://x/{version_major}-r/{version}/a-{version_compact}.html") == \
        "https://x/%-r/%/a-%.html"
