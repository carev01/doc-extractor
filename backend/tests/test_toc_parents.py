"""Pure unit tests for _resolve_toc_parents — TOC parent linkage, no DB required."""

from app.services.firecrawl import _resolve_toc_parents


def test_uses_parent_url_when_present():
    """parent_url wins, so hierarchy is exact even if levels look ambiguous."""
    entries = [
        {"url": "/root", "level": 0, "parent_url": None},
        {"url": "/a", "level": 1, "parent_url": "/root"},
        {"url": "/a1", "level": 2, "parent_url": "/a"},
        {"url": "/b", "level": 1, "parent_url": "/root"},
    ]
    assert _resolve_toc_parents(entries) == [None, 0, 1, 0]


def test_multi_position_page_does_not_scramble_siblings():
    """A page reachable from two parents is deduped to its first occurrence; later
    children must still attach to their real parent via parent_url, not to whatever
    happened to precede them in the flattened, gap-ridden list."""
    # /shared appears under /sectionA (kept) and would also appear under /sectionB.
    entries = [
        {"url": "/sectionA", "level": 0, "parent_url": None},
        {"url": "/shared", "level": 1, "parent_url": "/sectionA"},
        {"url": "/sectionB", "level": 0, "parent_url": None},
        {"url": "/b-child", "level": 1, "parent_url": "/sectionB"},
    ]
    parents = _resolve_toc_parents(entries)
    # /b-child must hang off /sectionB (index 2), not /sectionA — even though the
    # last level-0 entry seen by pure adjacency would also be /sectionB here, the
    # parent_url guarantees it regardless of intervening dedup gaps.
    assert parents == [None, 0, None, 2]


def test_falls_back_to_level_adjacency_without_parent_url():
    """Profiles that only carry depth (no parent_url) keep level-adjacency."""
    entries = [
        {"url": "/root", "level": 0, "parent_url": None},
        {"url": "/a", "level": 1, "parent_url": None},
        {"url": "/a1", "level": 2, "parent_url": None},
        {"url": "/b", "level": 1, "parent_url": None},
    ]
    assert _resolve_toc_parents(entries) == [None, 0, 1, 0]


def test_parent_url_missing_target_falls_back_to_level():
    """If parent_url points to something not in the list, don't orphan — use level."""
    entries = [
        {"url": "/root", "level": 0, "parent_url": None},
        {"url": "/a", "level": 1, "parent_url": "/does-not-exist"},
    ]
    assert _resolve_toc_parents(entries) == [None, 0]


def test_parent_always_precedes_child():
    entries = [
        {"url": "/root", "level": 0, "parent_url": None},
        {"url": "/x", "level": 1, "parent_url": "/root"},
        {"url": "/y", "level": 2, "parent_url": "/x"},
    ]
    for i, p in enumerate(_resolve_toc_parents(entries)):
        assert p is None or p < i
