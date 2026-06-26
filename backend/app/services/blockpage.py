"""Bot-protection / interstitial page detection.

Some vendor sites (some behind Akamai, others behind Cloudflare/Imperva) reject
our scraper egress with a short "Access Denied" / challenge page instead of the
real content. That page is non-empty, so without this guard it sails past the
empty-content check and gets stored as if it were a legitimate article — silently
corrupting the source (observed: a 279-byte Akamai "Access Denied" page stored as
the sole "article" of a support-manual guide, run reported COMPLETED).

``is_block_page`` recognises the common block/challenge fingerprints. It is
deliberately conservative: long pages are only flagged by markers that
essentially never occur in real documentation (e.g. ``edgesuite.net``,
``cf-browser-verification``); generic "access denied" phrasing only counts on a
short page, so a genuine doc *about* access-denied errors isn't misclassified.
"""

import re

# "Reference #18.8c42…" — the id Akamai stamps on its denial page.
_AKAMAI_REF_RE = re.compile(r"reference\s*#\s*\d", re.IGNORECASE)

# Markers specific enough to flag a page of any length — these are CDN/WAF
# challenge artefacts that don't appear in real product documentation.
_STRONG_MARKERS = (
    "edgesuite.net",                    # Akamai error CDN host on its denial page
    "cf-browser-verification",          # Cloudflare interstitial
    "/cdn-cgi/challenge-platform",      # Cloudflare managed challenge
    "attention required! | cloudflare",
    "incapsula incident id",            # Imperva Incapsula
    "request unsuccessful. incapsula",
    "pardon our interruption",          # Imperva/Distil bot wall
)

# Phrases that indicate a block only when the page is short (a real article that
# merely mentions these would be far longer and structured).
_SHORT_PAGE_LIMIT = 800
_SHORT_MARKERS = (
    "you don't have permission to access",
    "access denied",
    "request unsuccessful",
)


def is_block_page(text: str) -> bool:
    """Return True if *text* looks like a bot-protection / WAF block or challenge
    page rather than real content. Accepts markdown or plain text."""
    if not text:
        return False
    low = text.lower()

    if any(m in low for m in _STRONG_MARKERS):
        return True

    stripped = text.strip()
    if len(stripped) <= _SHORT_PAGE_LIMIT:
        if any(m in low for m in _SHORT_MARKERS) and (
            _AKAMAI_REF_RE.search(low) or "permission" in low or "denied" in low
        ):
            return True
        # Cloudflare's JS interstitial ("Just a moment…", "Enable JavaScript…").
        if "just a moment" in low and "javascript" in low:
            return True

    return False


# Login-host fragments: if the *final* URL lands on one of these, the scrape was
# bounced to an identity provider / login page rather than the docs content.
_LOGIN_HOST_MARKERS = (
    "login", "signin", "sign-in", "oauth2", "/authorize", "b2clogin.com",
    "onepassport", "auth0.com", "okta.com", "/saml", "accounts.google.com",
)

# Auth-wall phrases. Short-page-gated like _SHORT_MARKERS so a real article that
# documents authentication isn't misflagged.
_AUTH_WALL_MARKERS = (
    "requires authentication",
    "please sign in to continue",
    "you must be logged in to view",
    "session has expired",
)


def is_auth_wall(text: str, final_url: str | None = None,
                 login_domain: str | None = None) -> bool:
    """Return True if a scrape was bounced to a login wall / IdP rather than
    returning documentation content."""
    if final_url:
        low_url = final_url.lower()
        if any(m in low_url for m in _LOGIN_HOST_MARKERS):
            return True
    if text:
        low = text.lower()
        if len(text.strip()) <= _SHORT_PAGE_LIMIT and any(m in low for m in _AUTH_WALL_MARKERS):
            return True
    return False
