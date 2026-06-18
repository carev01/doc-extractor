"""Generic sitemap-based extraction profile — best-effort fallback.

This profile is **never auto-selected by detection** (``detect`` always returns
False).  It is chosen only as an explicit platform override (``source.platform =
"generic"``) or as the resolver's final fallback when no specific profile is
detected.

TOC construction relies on :meth:`Scraper.map_urls`, which calls the Firecrawl
``/v2/map`` endpoint and falls back to ``/sitemap.xml``.  Because sitemaps are
flat (no parent/child metadata), the TOC hierarchy is derived from URL path
depth relative to *root_url*.  **Document order from the sitemap/map is
preserved but does not necessarily reflect the actual site TOC order** — this
is an inherent limitation of generic sitemap-based discovery.
"""

from urllib.parse import urlparse

from app.services.profiles import registry
from app.services.profiles.base import TocEntry


def _path_segments(url: str) -> list[str]:
    """Return the non-empty path segments of *url*."""
    return [s for s in urlparse(url).path.split("/") if s]


class GenericProfile:
    name = "generic"

    def detect(self, root_html: str, root_url: str) -> bool:
        """Always returns False — this profile is never auto-selected."""
        return False

    async def build_toc(self, root_url: str, scraper) -> list[TocEntry]:
        """Build a best-effort TOC from the site map / sitemap.

        Steps:
        1. Fetch all URLs via ``scraper.map_urls(root_url)``.
        2. Keep only URLs whose path starts with *root_url*'s directory prefix
           (same host, path prefix match).  This filters out unrelated site
           sections (e.g. marketing pages) when the docs live under a subpath.
        3. Derive hierarchy from URL path depth relative to *root_url*'s depth:
           ``level = len(url_segments) - len(root_segments)``, floored at 0.
        4. Assign ``parent_url`` = the URL formed by dropping the last path
           segment, **only** if that URL is itself in the kept set.
        5. De-duplicate (first occurrence wins; document order preserved).

        All entries are marked ``is_article=True``; the generic profile has no
        way to distinguish section headers from content pages from a flat URL
        list alone.
        """
        all_urls: list[str] = await scraper.map_urls(root_url)

        root_parsed = urlparse(root_url)
        root_host = root_parsed.netloc
        root_segs = _path_segments(root_url)
        # The "directory" prefix is everything up to (but not including) the
        # last path component.  For a root like https://x/docs/ the prefix is
        # /docs/; for https://x/docs/index.html the prefix is /docs/.
        root_path = root_parsed.path
        # Normalise: treat the root path as a directory
        if not root_path.endswith("/"):
            # Drop the last component (filename / slug) to get the dir prefix
            root_path = root_path.rsplit("/", 1)[0] + "/"

        # Derive the baseline depth from the *normalised* directory prefix so
        # that a file-tailed root (e.g. /docs/index.html → dir /docs/) gives the
        # same baseline as a slash-terminated root (/docs/).  Using root_segs
        # (the raw URL segments) would over-count by one for file-tailed roots.
        root_dir_depth = len([s for s in root_path.split("/") if s])

        # Build the kept set (for parent_url lookup) and deduplicated list
        kept_ordered: list[str] = []
        kept_set: set[str] = set()
        for url in all_urls:
            if url in kept_set:
                continue
            p = urlparse(url)
            if p.netloc != root_host:
                continue
            if not p.path.startswith(root_path):
                continue
            kept_set.add(url)
            kept_ordered.append(url)

        toc: list[TocEntry] = []
        for url in kept_ordered:
            url_segs = _path_segments(url)
            level = max(0, len(url_segs) - root_dir_depth)

            # Parent = same URL with last path segment dropped, if in kept set.
            # Check both the slash-terminated form (/docs/a/) and the bare form
            # (/docs/a) because sitemaps may list URLs either way.
            parent_url: str | None = None
            if url_segs and len(url_segs) > root_dir_depth:
                parsed_url = urlparse(url)
                scheme_host = f"{parsed_url.scheme}://{parsed_url.netloc}"
                parent_path_bare = "/" + "/".join(url_segs[:-1])
                parent_candidate_slash = f"{scheme_host}{parent_path_bare}/"
                parent_candidate_bare = f"{scheme_host}{parent_path_bare}"
                if parent_candidate_slash in kept_set:
                    parent_url = parent_candidate_slash
                elif parent_candidate_bare in kept_set:
                    parent_url = parent_candidate_bare

            # Derive a human-readable title from the last path segment
            title = url_segs[-1] if url_segs else url

            toc.append(TocEntry(
                title=title,
                url=url,
                level=level,
                is_article=True,
                parent_url=parent_url,
            ))

        return toc

    def content_config(self) -> dict:
        return {"onlyMainContent": True, "waitFor": 1500}


PROFILE = GenericProfile()
registry.register(PROFILE)
