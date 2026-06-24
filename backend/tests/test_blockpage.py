"""Tests for bot-protection / WAF block-page detection."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.blockpage import is_block_page

# The exact Akamai page that was silently stored as a Dell "article".
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
