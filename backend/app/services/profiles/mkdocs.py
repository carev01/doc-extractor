"""MkDocs Material documentation profile.

TOC: parse the nested sidebar nav via sidebar_tree_toc using the
``.md-nav--primary .md-nav__list`` selector, which targets the top-level
``<ul class="md-nav__list">`` inside the primary sidebar nav.

MkDocs Material wraps nested child navs as:
  <li class="md-nav__item--nested">
    <label class="md-nav__link">Section</label>
    <nav class="md-nav"><ul class="md-nav__list">…children…</ul></nav>
  </li>

The shared ``sidebar_tree_toc`` helper uses ``li.find("ul") or li.find("ul")``
(fallback to any descendant) to correctly find the wrapped child list.

Content: ``article.md-content__inner`` — the Material article wrapper.
"""

from app.services.profiles import registry
from app.services.profiles.strategies import sidebar_tree_toc
from app.services.profiles.base import TocEntry


class MkDocsProfile:
    name = "mkdocs"

    def detect(self, root_html: str, root_url: str) -> bool:
        return "md-nav__list" in root_html and "md-content" in root_html

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        return await sidebar_tree_toc(
            scraper, root_url, ".md-nav--primary .md-nav__list"
        )

    def content_config(self) -> dict:
        return {
            "includeTags": ["article.md-content__inner"],
            "onlyMainContent": False,
            "waitFor": 1500,
        }


PROFILE = MkDocsProfile()
registry.register(PROFILE)
