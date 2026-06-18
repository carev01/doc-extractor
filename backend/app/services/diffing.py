"""Diff utilities for comparing article versions."""

import difflib


def compute_unified_diff(
    old: str,
    new: str,
    from_label: str = "previous",
    to_label: str = "current",
) -> str:
    """Return a unified diff between two markdown strings.

    Used as a fallback when a stored ``diff_text`` is unavailable — e.g. for
    versions captured via the hash-comparison path (no Firecrawl API key), which
    don't carry a git-diff from changeTracking.
    """
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=from_label,
        tofile=to_label,
    )
    return "".join(diff)
