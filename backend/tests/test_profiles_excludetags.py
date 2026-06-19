"""Each platform profile drops its known in-page chrome via excludeTags.

These are the scrape-time half of the sanitization (the post-process half in
services/sanitize.py applies to every profile). Profiles whose includeTags
already scope tightly to the content body (salesforce, confluence, commvault)
intentionally have no excludeTags — chrome never enters the scrape.
"""

import pytest

from app.services.profiles.flare_html5 import FlareHtml5Profile
from app.services.profiles.flare_webhelp import FlareWebHelpProfile
from app.services.profiles.docusaurus import DocusaurusProfile
from app.services.profiles.mkdocs import MkDocsProfile
from app.services.profiles.gitbook import GitBookProfile
from app.services.profiles.intercom import IntercomProfile
from app.services.profiles.freshdesk import FreshdeskProfile

_EXPECTED = {
    FlareHtml5Profile: [".GoToTop", ".feedback-button", ".nocontent"],
    FlareWebHelpProfile: [".GoToTop", ".feedback-button", ".nocontent"],
    DocusaurusProfile: [
        ".theme-edit-this-page", ".pagination-nav",
        ".theme-doc-footer", ".theme-doc-breadcrumbs", ".theme-last-updated",
    ],
    MkDocsProfile: [".md-feedback", ".md-source-file", ".md-content__button"],
    GitBookProfile: [
        "[data-testid='page-feedback']",
        "[data-testid='page-footer-navigation']",
    ],
    IntercomProfile: [
        ".intercom-interblocks-article-reactions",
        ".intercom-interblocks-related-articles",
    ],
    FreshdeskProfile: [".article-votes", ".vote-options", ".related-articles"],
}


@pytest.mark.parametrize("profile_cls,expected", _EXPECTED.items())
def test_profile_exclude_tags(profile_cls, expected):
    assert profile_cls().content_config().get("excludeTags") == expected
