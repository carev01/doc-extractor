"""Commvault documentation profile.

The current documentation.commvault.com platform renders its sidebar nav
client-side and lazy-loads each branch only when expanded, so neither static
HTML nor a one-shot render captures the full tree. We reproduce the real TOC by
**depth-first expanding the sidebar in Browserless** — clicking each parent's
toggle to reveal its children and recursing (``scraper.expand_toc``). This gives
the exact site hierarchy.

* Rooted at ``index.html`` → the whole product doc set.
* Rooted at a specific page → that section's subtree (scoped bookshelf).

Some nodes (e.g. "Protect", "Identity") are categories with a toggle but no page
of their own; they become url-less section entries that their children nest
under. Article content is server-rendered in ``#doc`` (Firecrawl content path).
"""

from urllib.parse import urljoin, urlparse

from app.services.profiles import registry
from app.services.profiles.base import TocEntry


class CommvaultProfile:
    name = "commvault"

    def detect(self, root_html: str, root_url: str) -> bool:
        # New platform: documentation.commvault.com (nav is "Loading…" client-side,
        # so key off the host / cv- markers). Old platform: inline #nav + nav-group.
        host = urlparse(root_url).netloc
        if host.endswith("documentation.commvault.com") or "cv-nav-slug" in root_html:
            return True
        return 'id="nav"' in root_html and "nav-group" in root_html

    def content_config(self) -> dict:
        # #doc holds the article, but starts with a ">" breadcrumb trail; drop it.
        return {
            "includeTags": ["#doc"],
            "excludeTags": [".breadcrumbs"],
            "onlyMainContent": False,
            "waitFor": 1500,
        }

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        """Depth-first expand the sidebar (via Browserless) into an ordered TOC.

        Mirrors the proven standalone Playwright crawler: load the page, then walk
        the tree with cheap in-page toggle clicks (~200ms each). One full session
        captures the whole ~9,670-node tree in ~14 min.

        * Specific page → that section's ``<li id>`` (``nav__<page-key>``) subtree,
          in a single session.
        * index.html with a checkpoint store → the whole product doc set, expanded
          **one top-level section per Browserless session, sequentially**, each
          persisted as it completes so an interrupted run resumes from the
          sections already done (see ``_build_full_resumable``). Sequential (not
          concurrent) avoids overrunning Browserless's session concurrency, which
          previously starved big sections into empty stubs.
        * index.html without a checkpoint (tests) → one full session.
        """
        root_file = urlparse(root_url).path.rsplit("/", 1)[-1]

        if root_file.endswith(".html") and root_file != "index.html":
            section_id = "nav__" + root_file[:-5]
            return self._nodes_to_toc(await scraper.expand_toc(root_url, section_id=section_id), root_url)

        checkpoint = getattr(scraper, "checkpoint", None)
        if checkpoint is None:
            return self._nodes_to_toc(await scraper.expand_toc(root_url), root_url)

        return self._nodes_to_toc(await self._build_full_resumable(root_url, scraper, checkpoint), root_url)

    @staticmethod
    async def _build_full_resumable(root_url: str, scraper, checkpoint) -> list[dict]:
        """Expand every top-level section sequentially, checkpointing each.

        On resume, sections already in the checkpoint are reused (not re-walked);
        only the remaining ones are expanded. The checkpoint is cleared once the
        whole tree is assembled.
        """
        state = await checkpoint.load()
        tops = state.get("top_level")
        if not tops:
            tops = await scraper.expand_toc(root_url, section_id="__TOP__")
            await checkpoint.save_top_level(tops)
        done = state.get("sections") or {}

        all_nodes: list[dict] = []
        for top in tops:
            sid = top.get("id")
            if sid in done:
                nodes = done[sid]
            elif top.get("isParent") and sid:
                nodes = await scraper.expand_toc(root_url, section_id=sid)
                # Keep at least the section's own node if it expanded to nothing.
                nodes = nodes or [{"id": sid, "href": top.get("href"),
                                   "title": top.get("title"), "level": 0,
                                   "isParent": top.get("isParent")}]
                await checkpoint.save_section(sid, nodes)
            else:
                nodes = [{"id": sid, "href": top.get("href"), "title": top.get("title"),
                          "level": 0, "isParent": top.get("isParent")}]
            all_nodes.extend(nodes)

        await checkpoint.clear()
        return all_nodes

    @staticmethod
    def _nodes_to_toc(nodes: list[dict], root_url: str) -> list[TocEntry]:
        """Map ordered {href,title,level,isParent} nodes to hierarchical TocEntry.

        parent_url comes from a level stack; category nodes (no href) are url-less
        sections, so their children fall back to level adjacency downstream.
        """
        out: list[TocEntry] = []
        level_url: dict[int, str | None] = {}  # level -> url (or None) of last node
        for n in nodes:
            title = (n.get("title") or "").strip()
            if not title:
                continue
            try:
                level = max(0, int(n.get("level", 0)))
            except (ValueError, TypeError):
                level = 0
            href = n.get("href")
            url = urljoin(root_url, href) if href else None
            parent_url = level_url.get(level - 1) if level > 0 else None

            level_url[level] = url
            for deeper in [k for k in level_url if k > level]:
                del level_url[deeper]

            out.append(TocEntry(
                title=title, url=url, level=level,
                is_article=bool(url), parent_url=parent_url,
            ))
        return out


PROFILE = CommvaultProfile()
registry.register(PROFILE)
