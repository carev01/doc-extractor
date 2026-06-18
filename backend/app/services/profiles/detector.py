"""Platform detector: iterate registered profiles and return the first match.

Usage::

    from app.services.profiles.detector import detect_platform

    name = detect_platform(root_html, root_url)
    # returns a profile name str, or None if no profile matched
"""

from app.services.profiles import registry


def detect_platform(root_html: str, root_url: str) -> str | None:
    """Return the name of the first registered profile whose detect() is True.

    Iterates ``registry.PROFILES`` in registration order (detection priority is
    controlled by import order in ``profiles/__init__.py``).

    Returns None when no profile matches, signalling that the caller should fall
    back to a default.
    """
    for profile in registry.PROFILES:
        try:
            if profile.detect(root_html, root_url):
                return profile.name
        except Exception:
            # A buggy detect() must not abort the whole detection loop.
            continue
    return None
