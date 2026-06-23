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


def test_removes_leading_you_are_here_breadcrumb():
    """Salesforce Help articles open with a 'You are here:' breadcrumb above the title."""
    md = (
        "You are here:\n"
        "\n"
        "1. [Salesforce Help](/s/?language=en_US)\n"
        "2. [Docs](/s/products?language=en_US)\n"
        "3. [Own from Salesforce](https://help.salesforce.com/s/articleView?id=platform.own.htm)\n"
        "\n"
        "User Roles\n"
        "==========\n"
        "\n"
        "Roles are assigned to users per business unit.\n"
    )
    out = sanitize_markdown(md)
    assert "You are here" not in out
    assert "Salesforce Help](/s/" not in out  # breadcrumb links gone
    assert out.lstrip().startswith("User Roles")
    assert "Roles are assigned to users per business unit." in out


def test_keeps_prose_mentioning_you_are_here_mid_document():
    """The breadcrumb rule is leading-anchored — mid-article text is untouched."""
    md = (
        "Guide\n=====\n\n"
        "The banner shows you are here in the workflow.\n"
        "More content.\n"
    )
    out = sanitize_markdown(md)
    assert "you are here in the workflow" in out


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


def test_removes_table_form_copyright_footer():
    """Landing/home pages render the footer inside a markdown table cell."""
    md = (
        "Welcome to Datto SaaS Protection\n"
        "================================\n"
        "\n"
        "The Online Help provides the information you need.\n"
        "\n"
        "[Datto SaaS Protection Release Notes](https://saasprotection.datto.com/help/Content/release-landing/saas-protection-rn.htm)\n"
        "\n"
        "|     |     |\n"
        "| --- | --- | \n"
        "| Copyright © 2026 Kaseya \\| [Privacy Policy](https://www.kaseya.com/legal/kaseya-privacy-statement/)<br> \\| [Cookies Settings](https://x#)<br> \\| [Website Terms of Use](https://www.kaseya.com/legal/website-terms-of-use/) |     |\n"
    )
    out = sanitize_markdown(md)
    assert "Copyright ©" not in out
    assert "Website Terms of Use" not in out
    assert "Privacy Policy" not in out
    # The whole footer table is gone — no dangling empty table rows.
    assert "| --- |" not in out
    assert "|     |     |" not in out
    # Real content above the footer is preserved.
    assert "Welcome to Datto SaaS Protection" in out
    assert "Release Notes]" in out


def test_real_table_above_footer_is_preserved():
    """A genuine data table separated from the footer must not be consumed."""
    md = (
        "Specs\n"
        "=====\n"
        "\n"
        "| Feature | Value |\n"
        "| --- | --- |\n"
        "| Retention | 1 year |\n"
        "\n"
        "|     |     |\n"
        "| --- | --- |\n"
        "| Copyright © 2026 Kaseya \\| [Privacy Policy](https://x) |     |\n"
    )
    out = sanitize_markdown(md)
    assert "Copyright ©" not in out
    assert "| Feature | Value |" in out
    assert "Retention" in out


def test_removes_helpful_widget_variants():
    """Other platforms phrase the feedback widget differently."""
    for heading, answer in [
        ("Was this page helpful?", "Yes No"),
        ("Was this article helpful?", "Yes / No"),
        ("Did this answer your question?", "Yes No"),
        ("Did you find this helpful?", "👍 👎"),
        ("**Was this doc helpful?**", "Thanks for your feedback!"),
    ]:
        md = f"Title\n=====\n\nReal content here.\n\n{heading}\n\n{answer}\n"
        out = sanitize_markdown(md)
        assert "Real content here." in out
        assert "helpful" not in out.lower() and "answer your question" not in out.lower()


def test_removes_vote_tally_and_thanks():
    md = (
        "Guide\n=====\n\nUseful body.\n\n"
        "Was this helpful?\n\n"
        "12 out of 15 found this helpful\n\n"
        "Thanks for your feedback!\n"
    )
    out = sanitize_markdown(md)
    assert "Useful body." in out
    assert "found this helpful" not in out
    assert "Thanks for your feedback" not in out


def test_removes_edit_this_page_links():
    md = (
        "Topic\n=====\n\nReal documentation.\n\n"
        "[Edit this page](https://github.com/org/repo/edit/main/docs/topic.md)\n"
    )
    out = sanitize_markdown(md)
    assert "Real documentation." in out
    assert "Edit this page" not in out


def test_prose_mentioning_helpful_is_kept():
    """A real sentence that merely contains 'helpful' must not be dropped."""
    md = (
        "Tips\n====\n\n"
        "This setting is helpful when you have many tenants to manage.\n"
        "Was this configuration applied? Check the status page.\n"
    )
    out = sanitize_markdown(md)
    assert "helpful when you have many tenants" in out
    assert "Was this configuration applied?" in out


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


# Real captured tail from a stored Flosum (GitBook) article: prev/next nav, the
# relative "Last updated" line, then the cookie/privacy-consent banner.
FLOSUM_TAIL = (
    "This article outlines the required prerequisites for Flosum Backup & Archive.\n"
    "\n"
    "[PreviousBackup & Archive - Overview](https://docs.flosum.com/backup-and-archive)\n"
    "[NextHow to Start a Free Backup & Archive Trial](https://docs.flosum.com/backup-and-archive/getting-started/how-to-start-a-free-backup-and-archive-trial)\n"
    "\n"
    "Last updated 1 month ago\n"
    "\n"
    "This site uses cookies to deliver its service and to analyze traffic. By browsing this site, you accept the [privacy policy](https://www.flosum.com/privacy-policy)\n"
    ".\n"
    "\n"
    "AcceptReject\n"
)


def test_removes_cookie_consent_banner():
    out = sanitize_markdown(FLOSUM_TAIL)
    assert "uses cookies" not in out
    assert "privacy policy" not in out
    assert "AcceptReject" not in out
    # The wrapped lone "." that followed the banner text is gone too.
    assert not any(ln.strip() == "." for ln in out.splitlines())


def test_removes_last_updated_footer():
    out = sanitize_markdown(FLOSUM_TAIL)
    assert "Last updated" not in out


# Real captured head from a stored GitBook (Flosum) article: the leading
# "llms.txt / available as Markdown" banner, then the real title + body.
FLOSUM_HEAD = (
    "For the complete documentation index, see [llms.txt](https://docs.flosum.com/llms.txt)\n"
    ". This page is also available as [Markdown](https://docs.flosum.com/backup-and-archive/getting-started/backup-and-archive-prerequisites.md)\n"
    ".\n"
    "\n"
    "![](/media/x/icon.svg) Overview\n"
    "\n"
    "----\n"
    "\n"
    "This article outlines the required prerequisites for Flosum Backup & Archive.\n"
)


def test_removes_leading_llms_markdown_banner():
    out = sanitize_markdown(FLOSUM_HEAD)
    assert "complete documentation index" not in out
    assert "llms.txt" not in out
    assert "also available as" not in out
    # Real content survives, and the banner didn't eat the title.
    assert "Overview" in out
    assert "required prerequisites for Flosum Backup & Archive" in out
    # The document now starts at the real content, not the banner.
    assert out.lstrip().startswith("![](/media/x/icon.svg) Overview")


def test_llms_banner_only_strips_at_document_head():
    """The signature mid-document (not at the head) is left alone — conservative."""
    md = (
        "# Real Title\n\n"
        "Body paragraph one.\n\n"
        "For the complete documentation index, see [llms.txt](https://x/llms.txt)\n"
    )
    out = sanitize_markdown(md)
    assert "complete documentation index" in out


def test_removes_prev_next_page_nav():
    out = sanitize_markdown(FLOSUM_TAIL)
    assert "PreviousBackup & Archive - Overview" not in out
    assert "NextHow to Start" not in out
    assert "docs.flosum.com/backup-and-archive)" not in out


def test_page_nav_preserves_real_links_with_space():
    """A genuine link whose text merely starts with 'Next '/'Previous ' (with a
    space) is real content and must survive."""
    md = (
        "See the guide below.\n\n"
        "[Next steps](https://x/next-steps)\n"
        "[Previous releases](https://x/releases)\n"
        "[Nextcloud integration](https://x/nextcloud)\n"
    )
    out = sanitize_markdown(md)
    assert "Next steps" in out
    assert "Previous releases" in out
    assert "Nextcloud integration" in out


def test_cookie_and_last_updated_preserve_prose():
    out = sanitize_markdown(FLOSUM_TAIL)
    assert "required prerequisites for Flosum Backup & Archive" in out


def test_last_updated_variants_and_italics():
    for line in (
        "Last updated 2 months ago",
        "Last updated 3 days ago",
        "_Last updated just a moment ago_",
        "Last updated 1 year ago",
    ):
        md = f"Real content here.\n\n{line}\n"
        out = sanitize_markdown(md)
        assert "Last updated" not in out, line
        assert "Real content here." in out


def test_cookie_button_link_form():
    """Accept/Reject may render as markdown links."""
    md = (
        "Body text.\n\n"
        "This site uses cookies to deliver its service. You accept the [privacy policy](https://x/p)\n"
        "\n"
        "[Accept](#a)[Reject](#r)\n"
    )
    out = sanitize_markdown(md)
    assert "uses cookies" not in out
    assert "Accept" not in out and "Reject" not in out
    assert "Body text." in out


def test_last_updated_does_not_eat_real_prose():
    """A sentence merely containing 'ago' must not be stripped."""
    md = "We released this feature a long time ago and it works well.\n"
    out = sanitize_markdown(md)
    assert "a long time ago and it works well" in out


# Real captured head from a stored Druva (Intercom-hosted) article: every page
# opens with a font/Apache-license preamble glued to a "Skip to main content"
# link, then the real content.
DRUVA_HEAD = (
    'Copyright 2023. Intercom Inc. Licensed under the Apache License, Version 2.0 '
    '(the "License"); you may not use this file except in compliance with the '
    'License. You may obtain a copy of the License at '
    'http://www.apache.org/licenses/LICENSE-2.0 Unless required by applicable law '
    'or agreed to in writing, software distributed under the License is distributed '
    'on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either '
    'express or implied. See the License for the specific language governing '
    'permissions and limitations under the License.Copyright (c) 2023, Intercom, '
    'Inc. (legal@intercom.io) with Reserved Font Name "Lato". This Font Software is '
    'licensed under the SIL Open Font License, Version 1.1.'
    '[Skip to main content](https://help.druva.com/en/collections/6094377#main-content)\n'
    "\n"
    "![](/media/x/d69ad44d9c5d.png)\n"
    "\n"
    "Druva Cloud Platform\n"
    "====================\n"
    "\n"
    "Getting started with Druva, configuration, and reporting\n"
)


def test_removes_leading_font_license_preamble():
    out = sanitize_markdown(DRUVA_HEAD)
    assert "Apache License" not in out
    assert "SIL Open Font License" not in out
    assert "Skip to main content" not in out
    # real content survives, and is now the document head
    assert out.startswith("![](/media/x/d69ad44d9c5d.png)")
    assert "Druva Cloud Platform" in out


def test_font_license_preserves_content_glued_after_skip_link():
    md = DRUVA_HEAD.split("\n", 1)[0].replace(
        "#main-content)", "#main-content)Real heading"
    ) + "\n\nbody\n"
    out = sanitize_markdown(md)
    assert out.startswith("Real heading")


def test_font_license_only_fires_at_head_on_signature():
    """A mid-prose mention of both license names must not be stripped."""
    md = (
        "# Licensing\n\nThis product is under the Apache License; fonts use the "
        "SIL Open Font License.\n"
    )
    assert sanitize_markdown(md).strip() == md.strip()


def test_empty_input():
    assert sanitize_markdown("") == ""
    assert sanitize_markdown("   \n  ") == "   \n  "
