"""The decorative-image filter skips theme/skin chrome, keeps real content images.

These repeat on every page (MadCap Flare skin assets, system icons, spacers), so
downloading them per-article was the dominant extraction-time cost.
"""

import pytest

from app.services.firecrawl import _BOILERPLATE_IMG_RE

BASE = "https://continuity.datto.com/help/"

# Real skin/system/spacer images seen in the logs — must be skipped.
SKIP = [
    BASE + "Skins/Default/Stylesheets/Images/transparent.gif",
    BASE + "Content/Resources/Images/_SystemImages/StylesheetImages/copy-h3.png",
    BASE + "Content/Resources/Images/_SystemImages/StylesheetImages/copy.png",
    BASE + "Content/Resources/Images/_SystemImages/MasterPageImages/helpdesk.png",
    BASE + "Content/Resources/Images/_SystemImages/MasterPageImages/feedback.png",
]

# Genuine documentation images — must be kept.
KEEP = [
    BASE + "Content/Resources/Images/screenshots/backup-dashboard.png",
    BASE + "Content/kb/siris-alto-nas/diagram.jpg",
    "https://other.example.com/help/img/transparent-overlay-feature.png",
]


@pytest.mark.parametrize("url", SKIP)
def test_decorative_images_are_skipped(url):
    assert _BOILERPLATE_IMG_RE.search(url) is not None


@pytest.mark.parametrize("url", KEEP)
def test_content_images_are_kept(url):
    assert _BOILERPLATE_IMG_RE.search(url) is None
