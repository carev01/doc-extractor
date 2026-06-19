"""Tests for sanitize_markdown — boilerplate removal that preserves real prose.

Fixtures are real captured tails/heads from stored Datto SaaS Protection
articles (MadCap Flare HTML5), where the boilerplate this strips actually
lives.
"""

from app.services.sanitize import sanitize_markdown

# A realistic article: real prose, then the recurring Datto/Kaseya chrome.
DATTO_TAIL = (
    "Kaseya 365 User: Getting started with Datto SaaS Protection\n"
    "===========================================================\n"
    "\n"
    "PERMISSIONS \xa0Master role in KaseyaOne and Administrator role.\n"
    "\n"
    "Getting up and running with Datto SaaS Protection is an easy process.\n"
    "\n"
    "| ![](/media/x/a.png) | [Need help? Submit a Kaseya Helpdesk request.](https://helpdesk.kaseya.com/hc/requests/new) |\n"
    "| --- | --- |\n"
    "| ![](/media/x/b.png) | [Want to talk about it? Head over to Kaseya Community.](https://community.kaseya.com/) |\n"
    "| ![](/media/x/c.png) | [Have a new feature idea? Visit the Kaseya Ideas portal.](https://community.kaseya.com/p/ideas-portal) |\n"
    "| ![](/media/x/d.png) | [Provide feedback for the Documentation team.](javascript:(function()%7BSendLinkByMail()%3B%7D)()%3B) |\n"
    "\n"
    "**Was this article helpful?**\n"
    "\n"
    "Yes\xa0No\n"
    "\n"
    "[](https://saasprotection.datto.com/help/M365/Content/getting-started.htm#Top)\n"
    "[^](https://saasprotection.datto.com/help/M365/Content/getting-started.htm#Top)\n"
    "\n"
    "Copyright © 2026 Kaseya | [Privacy Policy](https://www.kaseya.com/legal/privacy/)\n"
    " | [Cookies Settings](https://saasprotection.datto.com/help/M365/Content/getting-started.htm#)\n"
    " | [Website Terms of Use](https://www.kaseya.com/legal/website-terms-of-use/)\n"
)


def test_removes_helpful_widget():
    out = sanitize_markdown(DATTO_TAIL)
    assert "Was this article helpful" not in out
    assert "Yes\xa0No" not in out and "Yes No" not in out


def test_removes_copyright_footer():
    out = sanitize_markdown(DATTO_TAIL)
    assert "Copyright ©" not in out
    assert "Website Terms of Use" not in out
    assert "Cookies Settings" not in out


def test_removes_back_to_top_anchors():
    out = sanitize_markdown(DATTO_TAIL)
    assert "#Top)" not in out


def test_removes_feedback_link_table():
    out = sanitize_markdown(DATTO_TAIL)
    assert "Submit a Kaseya Helpdesk request" not in out
    assert "SendLinkByMail" not in out
    assert "Visit the Kaseya Ideas portal" not in out


def test_preserves_real_prose():
    out = sanitize_markdown(DATTO_TAIL)
    assert "Getting started with Datto SaaS Protection" in out
    assert "PERMISSIONS" in out
    assert "easy process" in out


def test_removes_leading_promo_banner():
    md = (
        "Kaseya’s latest product innovations are live. [Explore now](https://www.kaseya.com/2026-h1-release)\n"
        ".\n"
        "\n"
        "Kaseya Help systems\n"
        "===================\n"
        "\n"
        "Filter by any of the following categories to access help systems.\n"
    )
    out = sanitize_markdown(md)
    assert "product innovations are live" not in out
    assert "Explore now" not in out
    assert out.lstrip().startswith("Kaseya Help systems")
    assert "Filter by any of the following categories" in out


def test_idempotent():
    once = sanitize_markdown(DATTO_TAIL)
    twice = sanitize_markdown(once)
    assert once == twice


def test_clean_article_unchanged_except_trailing_newline():
    md = (
        "Real Title\n"
        "==========\n"
        "\n"
        "Some genuinely useful documentation content with a [link](https://example.com/page).\n"
        "\n"
        "- step one\n"
        "- step two\n"
    )
    out = sanitize_markdown(md)
    assert "Real Title" in out
    assert "step one" in out and "step two" in out
    assert "useful documentation content" in out


def test_inline_toc_anchor_list_is_preserved():
    """On-page anchor lists are intentionally NOT stripped — they can be useful."""
    md = (
        "Overview\n"
        "========\n"
        "\n"
        "This article includes the following supplemental information:\n"
        "\n"
        "*   [Restricted regions](https://x.com/help/page.htm#Restricted_regions)\n"
        "*   [Browser requirements](https://x.com/help/page.htm#Browser_requirements)\n"
        "\n"
        "Body text follows.\n"
    )
    out = sanitize_markdown(md)
    assert "supplemental information" in out
    assert "Restricted regions" in out
    assert "Browser requirements" in out


def test_mid_article_copyright_not_treated_as_footer():
    """A 'Copyright' mention well above the tail must not nuke trailing content."""
    md = (
        "Licensing\n"
        "=========\n"
        "\n"
        "Copyright © 2026 Acme governs use of this software.\n"
        "\n"
        + "\n".join(f"Real paragraph {i} with substantive content." for i in range(20))
        + "\n"
    )
    out = sanitize_markdown(md)
    assert "Real paragraph 19" in out
    assert "Copyright © 2026 Acme governs" in out


def test_empty_input():
    assert sanitize_markdown("") == ""
    assert sanitize_markdown("   \n  ") == "   \n  "
