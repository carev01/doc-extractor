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
``cf-browser-verification``).

A short page needs more than the *phrase* "access denied". The rule used to be
"≤800 chars, mentions 'access denied', and mentions 'permission' or 'denied'" —
and the second half is always true once the first is, so every short page that
merely *mentions* access denied was dropped as a block. Short reference pages do
that constantly: every raw-HTTP page sitting in a blocked list in production was
real documentation (Cohesity NetBackup status codes such as "Message: VxSS access
denied", Arcserve troubleshooting and release notes, Commvault's KB "VNU0003 ...
fails with 'Access denied' error") — 50 to 115 words each, never stored, and
re-flagged on every run. A short page now counts only with real evidence of a
denial page: Akamai's reference id, the classic "you don't have permission to
access ... on this server" wording, or a page that is essentially nothing *but*
the denial (a handful of words — a bare "Access Denied", S3's AccessDenied XML).
"""

import re
from urllib.parse import urlparse

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

# Denial phrasing — suggestive on a short page, never conclusive on its own: real
# reference pages about permission errors are short too (see module docstring).
_SHORT_PAGE_LIMIT = 800
_SHORT_MARKERS = (
    "you don't have permission to access",
    "access denied",
    "request unsuccessful",
)
# A page that is *only* a denial is a handful of words (bare "Access Denied": 2;
# S3's AccessDenied XML: ~8; Akamai's page: ~25, and caught by edgesuite.net
# anyway). The shortest misflagged real doc page was 50 words.
_BARE_DENIAL_MAX_WORDS = 25


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
            # Akamai stamps its denial page with a reference id.
            _AKAMAI_REF_RE.search(low)
            # Apache/Akamai: 'You don't have permission to access "X" on this server.'
            or ("you don't have permission to access" in low and "on this server" in low)
            # Nothing but the denial itself.
            or len(stripped.split()) <= _BARE_DENIAL_MAX_WORDS
        ):
            return True
        # Cloudflare's JS interstitial ("Just a moment…", "Enable JavaScript…").
        if "just a moment" in low and "javascript" in low:
            return True

    return False


# Login-host fragments matched against the *hostname only*: the bare ones
# ("login"/"signin") would false-positive against a doc page whose path contains
# "login" (e.g. /configure-login-policy), so they must only count in the netloc.
_LOGIN_HOST_MARKERS = (
    "login", "signin", "sign-in", "b2clogin.com",
    "onepassport", "auth0.com", "okta.com", "accounts.google.com",
)

# IdP path fragments specific enough to flag anywhere in the URL.
_LOGIN_URL_MARKERS = ("oauth2", "/authorize", "/saml")

# Auth-wall phrases. Short-page-gated like _SHORT_MARKERS so a real article that
# documents authentication isn't misflagged.
_AUTH_WALL_MARKERS = (
    "requires authentication",
    "please sign in to continue",
    "you must be logged in to view",
    "session has expired",
)


# A client-side redirect shell: a meta refresh or a data-redirect-url attribute.
# Only read on a short raw page (_REDIRECT_SHELL_LIMIT), where it *is* the page.
_REDIRECT_TARGET_RE = re.compile(
    r"""(?:http-equiv=["']?refresh["']?[^>]*?content=["'][^"']*?url=['"]?([^"'>\s]+)"""
    r"""|data-redirect-url=["']([^"']+)["'])""",
    re.IGNORECASE,
)
_REDIRECT_SHELL_LIMIT = 2000
# Auth path fragments for a redirect *target* — too broad for an arbitrary final
# URL (a doc page may live at /sso/...), specific enough for where a 404-byte
# "Redirecting…" shell sends the browser.
_SSO_PATH_RE = re.compile(r"(?:^|[/_.-])(?:sso|auth|login|signin)(?:$|[/_.?-])")


def _is_login_url(url: str) -> bool:
    low_url = url.lower()
    netloc = urlparse(low_url).netloc
    return (any(m in netloc for m in _LOGIN_HOST_MARKERS)
            or any(m in low_url for m in _LOGIN_URL_MARKERS))


def _redirects_to_login(text: str) -> bool:
    """A short raw page whose only job is to send the browser to an auth endpoint.

    AvePoint Learn answers a request carrying an *expired* session cookie with a
    200, 404 bytes, and a meta refresh to ``/api/auth/aos-sso/…`` — the status,
    the URL and the visible text all look fine, so only the target gives it away
    (no cookie at all gets the public page, which is why it went unnoticed)."""
    if len(text) > _REDIRECT_SHELL_LIMIT:
        return False
    for m in _REDIRECT_TARGET_RE.finditer(text):
        target = m.group(1) or m.group(2)
        if _is_login_url(target) or _SSO_PATH_RE.search(urlparse(target.lower()).path):
            return True
    return False


def is_auth_wall(text: str, final_url: str | None = None,
                 login_domain: str | None = None) -> bool:
    """Return True if a scrape was bounced to a login wall / IdP rather than
    returning documentation content."""
    if final_url and _is_login_url(final_url):
        return True
    if text:
        low = text.lower()
        if len(text.strip()) <= _SHORT_PAGE_LIMIT and any(m in low for m in _AUTH_WALL_MARKERS):
            return True
        if _redirects_to_login(text):
            return True
    return False
