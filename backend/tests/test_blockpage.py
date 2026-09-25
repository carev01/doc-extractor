"""Tests for bot-protection / WAF block-page detection."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.blockpage import is_block_page

# The exact Akamai page that was silently stored as a support-manual "article".
AKAMAI_DENIED = (
    "Access Denied\n=============\n\n"
    "You don't have permission to access "
    '"http://www.dell.com/support/manuals/en-us/.../pp-dm_20.1_ag" on this server.\n\n'
    "Reference #18.87421502.1782318552.7eb3f1fe\n\n"
    "https://errors.edgesuite.net/18.87421502.1782318552.7eb3f1fe\n"
)


def test_detects_akamai_access_denied():
    assert is_block_page(AKAMAI_DENIED) is True


def test_detects_akamai_by_edgesuite_marker_even_if_long():
    # edgesuite.net is a strong marker — flagged regardless of length.
    padded = "lorem ipsum " * 200 + "https://errors.edgesuite.net/abc"
    assert is_block_page(padded) is True


def test_detects_cloudflare_interstitial():
    assert is_block_page(
        "Just a moment...\nPlease enable JavaScript and cookies to continue."
    ) is True


def test_detects_cloudflare_challenge_marker():
    assert is_block_page(
        "<html><script src='/cdn-cgi/challenge-platform/h/b/orchestrate'></script></html>"
    ) is True


def test_detects_imperva_incapsula():
    assert is_block_page("Request unsuccessful. Incapsula incident ID: 123-456") is True


def test_empty_is_not_block():
    assert is_block_page("") is False
    assert is_block_page("   \n  ") is False


def test_real_article_is_not_block():
    md = (
        "# Getting started\n\n"
        "PowerProtect Data Manager protects your data. This guide describes how to "
        "deploy, configure, and operate the appliance across your environment.\n\n"
        "## Overview\n\nThe system supports backup and recovery of assets...\n"
    )
    assert is_block_page(md) is False


def test_long_doc_mentioning_access_denied_is_not_block():
    # A genuine, long doc that happens to discuss "access denied" errors must not
    # be misclassified — the short-page guard prevents that.
    md = ("# Troubleshooting permissions\n\n"
          "If a user sees an 'Access Denied' message, check the role assignment. "
          "This section explains how permission errors arise and how to resolve them. ") * 20
    assert is_block_page(md) is False


# ── Short real documentation that merely mentions "access denied" ────────────
# The short-page rule used to accept "access denied" plus "permission" OR
# "denied" — always true once "access denied" matched — so every short page that
# mentioned it was dropped as a block, and re-dropped on every run. All raw-HTTP
# pages in production blocked lists were real docs like these (50-115 words).

def test_a_status_code_reference_page_is_not_a_block():
    md = ("NetBackup status code: 7418\n---------------------------\n\n"
          "**Message:** Access denied on target storage server.\n\n"
          "**Explanation:** Failed to perform the specified action of the target storage "
          "server.\n\n**Recommended Action:** See the error logs on the storage server and "
          "verify that the user has the required permissions, then retry the operation.")
    assert is_block_page(md) is False


def test_a_troubleshooting_kb_article_is_not_a_block():
    md = ("VNU0003: Client creation, subclient browse, or backup fails with “Access denied” error\n"
          "======================================================================================\n\n"
          "Symptom\n-------\n\nA configuration or browse operation fails with an access denied "
          "error on the client.\n\nResolution\n----------\n\nVerify the service account "
          "has local administrator rights and that UAC is not blocking the operation.")
    assert is_block_page(md) is False


def test_release_notes_mentioning_access_denied_are_not_a_block():
    md = ("Fixed Issues\n============\n\n* Fixed an issue where the Details pane reported "
          "the incorrect Failure Time.\n* Fixed an issue where consolidation failed with "
          "an access denied error on network shares.\n* Fixed an issue where the "
          "installer did not remove the old service.")
    assert is_block_page(md) is False


def test_a_permissions_note_quoting_the_akamai_sentence_is_not_a_block():
    """The Akamai sentence needs 'on this server' to count."""
    md = ("Console access\n==============\n\nIf you don't have permission to access the "
          "console, you will see an Access Denied message. Ask an administrator to add "
          "your account to the Operators role, then sign in again and reopen the page.")
    assert is_block_page(md) is False


# ── ...while genuine short denial pages are still caught ─────────────────────

def test_a_bare_access_denied_page_is_a_block():
    assert is_block_page("Access Denied") is True


def test_an_s3_access_denied_error_is_a_block():
    assert is_block_page("AccessDenied Access Denied RequestId 4F2A HostId abc123=") is True


def test_the_apache_denial_wording_is_a_block():
    assert is_block_page(
        "Forbidden\n\nYou don't have permission to access /docs/guide.html on this server.") is True


def test_akamai_without_its_edgesuite_link_is_still_caught_by_the_reference_id():
    assert is_block_page(
        "Access Denied\n\nYou don't have permission to access this page.\n\n"
        "Reference #18.87421502.1782318552.7eb3f1fe") is True
