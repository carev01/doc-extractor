"""URL templating for sources whose URL embeds the product version.

A source's ``url_template`` holds placeholders; the live ``base_url`` is the
template resolved against the product's current version. A ``topic_key`` is the
version-independent identity of an article — its URL with every volatile token
swapped back to a placeholder — so the same topic across versions shares one key
and its history continues across a version bump.

Two placeholders exist, and the difference between them is *who supplies the
value*:

``{version}``
    The product version, typed by an operator at bump time and stored on the
    Product. Appears in the URL verbatim.

``{rev}``
    An opaque per-release token the vendor mints and embeds in the URL — not
    derivable from the version and not known to the operator. Cohesity's
    NetBackup portal is the motivating case: a guide lives at
    ``/docs/netbackup/<version>/<publication>-<build>-0/<topic>-<build>``, where
    ``<publication>`` and ``<topic>`` are stable across versions but ``<build>``
    is re-minted per release (11.2's 171368441 became 11.2.0.1's 173151032) and
    appears **twice**. Templating only the version leaves the stale build id in
    both the URL (which 404s) and the topic key (which makes every article of the
    source look brand new, retiring the old corpus and re-adding it — the
    duplication class that the CommCell incident belongs to).

``{rev}`` is never stored resolved and never asked of the operator. It is read
off each URL *positionally* by :func:`extract_revision`, which needs no network
and no profile — deliberately, because key derivation is the load-bearing path:
an article must key stably even when the vendor's site is unreachable. The
network only enters when the source's own ``base_url`` has to be rebuilt for a
new release, which is a profile's job (``resolve_revision``) and is allowed to
fail without costing data.
"""

import re

VERSION_PLACEHOLDER = "{version}"
REVISION_PLACEHOLDER = "{rev}"

# Every placeholder the template language knows. Used to turn a topic key into a
# SQL LIKE pattern and to find the literal text that delimits a placeholder.
_PLACEHOLDER_RE = re.compile(r"\{(?:version|rev)\}")


def resolve_template(
    template: str, version: str, revision: str | None = None
) -> str:
    """Substitute the product version (and, when known, the revision token) into
    a URL template.

    Leaves ``{rev}`` in place when *revision* is None — callers that need a
    fetchable URL must supply one (or carry the previous release's, which at
    least stays well-formed until a run resolves the current one)."""
    out = template.replace(VERSION_PLACEHOLDER, version)
    if revision is not None:
        out = out.replace(REVISION_PLACEHOLDER, revision)
    return out


def _delimiter_after(tail: str) -> str | None:
    """The single literal character that terminates a ``{rev}`` token.

    *tail* is the slice of the template following the placeholder. Only its first
    character is used, never the whole literal run: the run belongs to *this*
    template (``-0/v95650213-``), but the token has to be read off sibling URLs
    under the same publication, whose topic ids differ (``-0/v95379219-``).
    A one-character delimiter is the longest prefix common to all of them.
    """
    if not tail:
        return None
    if _PLACEHOLDER_RE.match(tail):
        # Two placeholders butted together leave nothing to delimit with.
        return None
    return tail[0]


def extract_revision(
    url: str | None, url_template: str | None, version: str | None
) -> str | None:
    """Read the ``{rev}`` token off *url*, using *url_template* for its position.

    Anchored at the template's first ``{rev}`` placeholder: everything before it
    (with the version resolved) must prefix *url*, and the token is what follows
    up to the delimiter. Because the anchor is a prefix — for Cohesity
    ``…/<version>/<publication>-`` — this reads correctly for *every* article of
    the publication, not just the one the template was built from.

    Returns None whenever the shape doesn't hold, which leaves the caller with an
    un-templated literal rather than a wrong guess: a mis-read token would be
    substring-replaced across the whole URL and could corrupt the stable ids that
    carry the article's identity.
    """
    if not url or not url_template or not version:
        return None
    if REVISION_PLACEHOLDER not in url_template:
        return None
    head, _, tail = url_template.partition(REVISION_PLACEHOLDER)
    prefix = head.replace(VERSION_PLACEHOLDER, version)
    if not prefix or not url.startswith(prefix):
        return None
    rest = url[len(prefix):]
    delim = _delimiter_after(tail)
    if delim is None:
        # {rev} ends the template: the token runs to the end of the URL.
        return rest or None
    cut = rest.find(delim)
    if cut <= 0:
        return None
    return rest[:cut]


def like_pattern(topic_key: str) -> str:
    """A SQL LIKE pattern matching any concrete URL of *topic_key*'s shape.

    Placeholders become ``%``; LIKE metacharacters in the literal parts are
    escaped first (doc URLs commonly contain ``_``). Used to find a stored
    article across a release boundary, where neither its key nor its URL matches
    the freshly-derived ones.
    """
    esc = (
        topic_key.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return _PLACEHOLDER_RE.sub("%", esc)


def derive_topic_key(url: str | None, url_template: str | None, version: str | None) -> str | None:
    """Return the version-independent key for *url*.

    When the version is known, replace it with ``{version}`` — anchored at the
    template's placeholder offset when a ``url_template`` is available, else by a
    single substring replace. The result is stable regardless of whether
    ``url_template`` is set, so a missing/misconfigured template can never
    silently change an article's key and duplicate the whole source on
    re-extraction (the CommCell incident: a run with a NULL ``url_template`` keyed
    every page by its literal-version URL and re-created ~17.5k articles instead
    of matching the stored ``{version}`` keys). Only a missing version — a
    non-versioned source — leaves *url* untemplated.

    A ``{rev}``-bearing template additionally swaps the volatile per-release
    token (every occurrence — Cohesity repeats it), which is what lets a key
    survive a release whose URL changed in more than the version segment.

    *url* may be ``None`` for a url-less structural TOC node (a Flare "book"/
    section header that carries no page, common in HelpSystem.xml/Toc chunks);
    such entries have no topic identity, so it is returned unchanged rather than
    templated.
    """
    if not url or not version:
        return url
    key = url
    # Template-anchored replacement (preferred): swap exactly the version segment
    # at the template's placeholder offset.
    anchored = False
    if url_template and VERSION_PLACEHOLDER in url_template:
        prefix = url_template.split(VERSION_PLACEHOLDER, 1)[0]
        if url.startswith(prefix) and url[len(prefix):len(prefix) + len(version)] == version:
            key = prefix + VERSION_PLACEHOLDER + url[len(prefix) + len(version):]
            anchored = True
    # Fallback: templatize by the version substring even without a (matching)
    # template, so the key stays consistent when url_template is absent or the
    # version sits at an unexpected offset. Only when the version actually occurs.
    if not anchored and version in url:
        key = url.replace(version, VERSION_PLACEHOLDER, 1)

    # Then the volatile per-release token, read off *url* itself (see
    # extract_revision). Replaced everywhere it occurs, not once: Cohesity repeats
    # the build id in both the publication and the topic segment, and a key that
    # templated only the first would still change on the next release.
    revision = extract_revision(url, url_template, version)
    if revision:
        key = key.replace(revision, REVISION_PLACEHOLDER)
    return key


def detect_version_token(base_url: str, version: str) -> str | None:
    """Return a ``url_template`` (the first occurrence of *version* in *base_url*
    replaced by ``{version}``), or None when the version string isn't present."""
    if not version or version not in base_url:
        return None
    return base_url.replace(version, VERSION_PLACEHOLDER, 1)


def templatize(url: str, version: str, profile=None) -> str | None:
    """The best ``url_template`` for *url*: the profile's platform-aware one when
    it offers a ``templatize_url`` hook, else the generic version-only detection.

    Kept here rather than at each call site so enabling versioning, the sources
    API's detect endpoint and the auto-detection inside a run all produce the
    same template for the same URL — a source that got a version-only template
    from one path and a ``{rev}`` one from another would key its articles two
    different ways depending on which ran first.
    """
    if profile is not None:
        hook = getattr(profile, "templatize_url", None)
        if hook is not None:
            try:
                platform_template = hook(url, version)
            except Exception:
                platform_template = None
            if platform_template:
                return platform_template
    return detect_version_token(url, version)


def upgrade_template(
    url_template: str | None,
    base_url: str | None,
    version: str | None,
    profile=None,
) -> str | None:
    """A ``{rev}``-bearing replacement for a *version-only* template, or None.

    Sources templated before ``{rev}`` existed keep a template with the vendor's
    build id baked in as a literal. Nothing upgrades them on their own: the
    in-run auto-detection only fires when there is no template at all, and the
    UI only offers "Templatize" for an untemplated source. Left alone, such a
    source silently re-keys its whole corpus at the vendor's next release — the
    exact failure ``{rev}`` exists to prevent, still armed.

    Upgrading rewrites identity for every article of the source, so it is gated
    on a **round trip**: the profile's candidate template, resolved back against
    the version and the token read out of the current ``base_url``, must
    reproduce that ``base_url`` byte for byte. That proves the candidate
    describes this exact URL rather than merely resembling it, and it is what
    makes the upgrade safe to apply without operator review. Anything less
    certain returns None and leaves the old template in place.
    """
    if not url_template or not base_url or not version or profile is None:
        return None
    if REVISION_PLACEHOLDER in url_template:
        return None  # already upgraded
    hook = getattr(profile, "templatize_url", None)
    if hook is None:
        return None
    try:
        candidate = hook(base_url, version)
    except Exception:
        return None
    if not candidate or REVISION_PLACEHOLDER not in candidate:
        return None
    revision = extract_revision(base_url, candidate, version)
    if not revision:
        return None
    if resolve_template(candidate, version, revision) != base_url:
        return None
    return candidate


def _slug(text: str) -> str:
    """Lowercase, keep alphanumerics, collapse everything else to single hyphens."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s


def derive_pdf_topic_key(path: list[str]) -> str:
    """Stable topic key for a PDF article from its outline path (ancestor titles +
    own title). Slugged per segment and joined with "/" so re-converting the same
    PDF yields the same key — which keeps incremental diffs stable. Empty path
    (single-segment whole-document fallback) maps to "document"."""
    parts = [_slug(p) for p in path if _slug(p)]
    return "/".join(parts) if parts else "document"
