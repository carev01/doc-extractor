"""GitBook documentation profile.

TOC: GitBook renders its sidebar inside:
  <aside data-testid="table-of-contents" data-gb-table-of-contents="true">

The top-level <ul> contains two kinds of <li>:
  - Leaf articles:  <li><a href="...">Title</a></li>  (direct <a> child)
  - Sections:       <li>
                      <div>...</div>       (spacer / scroll sentinel)
                      <div><button>Section Name</button></div>
                      <div>...<ul>         (collapsed/expanded child list)
                        <li><a href="...">Child Title</a></li>
                        ...
                      </ul></div>
                    </li>

Because section <li>s have NO direct <a> child (only a <button> label), the
generic sidebar_tree_toc helper would incorrectly find the first *descendant*
anchor and produce duplicates.  We use a custom walk instead.

CSS classes are Tailwind / hashed and unstable; all selectors use the
stable ``data-testid`` / ``data-gb-table-of-contents`` attributes.

Content: GitBook pages render content inside <main>; ``onlyMainContent=True``
is the cleanest extraction path.  A longer ``waitFor`` (3 s) is used because
GitBook is an SPA that hydrates on the client.
"""

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.services.profiles import registry
from app.services.profiles.base import TocEntry


def _walk_gitbook_ul(ul, level: int, parent_url: str | None, root_url: str, out: list[TocEntry]) -> None:
    """Recursively walk a GitBook <ul> into TocEntry objects.

    GitBook top-level <li>s are either:
      - Leaf articles  — have a direct <a> child, no nested <ul>.
      - Sections       — have a <button> label and a descendant <ul> but NO
                         direct <a> child.  We treat the button text as the
                         section title and emit a non-article TocEntry, then
                         descend into the child <ul> at the next level.

    Any <li> with no <a> and no nested <ul> is skipped.
    """
    for li in ul.find_all("li", recursive=False):
        direct_a = li.find("a", recursive=False)
        child_ul = li.find("ul", recursive=False) or li.find("ul")

        if direct_a and direct_a.get("href"):
            # Leaf article or section-with-link
            url = urljoin(root_url, direct_a["href"])
            out.append(TocEntry(
                title=direct_a.get_text(strip=True),
                url=url,
                level=level,
                is_article=child_ul is None,
                parent_url=parent_url,
            ))
            if child_ul:
                _walk_gitbook_ul(child_ul, level + 1, url, root_url, out)
        elif child_ul:
            # Section with button label but no direct anchor.
            # Emit a non-article TocEntry using the button text; URL is empty
            # because GitBook section headers are expand/collapse buttons, not links.
            btn = li.find("button")
            section_title = btn.get_text(strip=True) if btn else ""
            if section_title:
                out.append(TocEntry(
                    title=section_title,
                    url="",
                    level=level,
                    is_article=False,
                    parent_url=parent_url,
                ))
            _walk_gitbook_ul(child_ul, level + 1, parent_url, root_url, out)
        # else: no anchor, no child ul — skip


class GitBookProfile:
    name = "gitbook"

    def detect(self, root_html: str, root_url: str) -> bool:
        return "data-gb-table-of-contents" in root_html

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        html = await scraper.get_html(root_url, 3000)
        soup = BeautifulSoup(html, "html.parser")
        aside = soup.select_one('aside[data-testid="table-of-contents"]')
        out: list[TocEntry] = []
        if not aside:
            return out
        top_ul = aside.find("ul")
        if not top_ul:
            return out
        _walk_gitbook_ul(top_ul, 0, None, root_url, out)
        return out

    def content_config(self) -> dict:
        return {
            # No includeTags (onlyMainContent heuristic), so drop GitBook's
            # in-page chrome explicitly: the "Was this helpful?" widget and the
            # prev/next page footer navigation. No-op when absent.
            "excludeTags": [
                "[data-testid='page-feedback']",
                "[data-testid='page-footer-navigation']",
            ],
            "onlyMainContent": True,
            "waitFor": 3000,
        }


PROFILE = GitBookProfile()
registry.register(PROFILE)
