"""Post-process sanitisation of scraped article markdown.

Removes recurring site chrome/boilerplate that adds no documentation value,
across documentation platforms — "Was this page helpful?" / "Did this answer
your question?" feedback widgets and their Yes/No / vote-tally / "Thanks for
your feedback" responses, "Edit this page" / "Edit on GitHub" links, back-to-top
anchors, copyright footers, feedback-link tables, and leading marketing banners
— while staying conservative:

* Rules are anchored to the document head/tail and match specific boilerplate
  signatures, never generic prose.
* Inline / on-page TOCs (anchor lists) are intentionally left untouched: they
  can carry useful structure and are not safely separable from real content.
* Safety comes from each rule being tail/head-anchored and matched to a
  specific signature (e.g. the copyright rule only fires in the last few
  lines, so it can never swallow a long body).  As a final backstop, if the
  pass somehow empties the article, the original is returned unchanged.  A
  short article that is mostly boilerplate is *expected* to shrink a lot —
  shrinkage alone is not treated as an error.

Applied at write time (see ``FirecrawlService.process_article_result``), so it
cleans content as it is stored.  Existing articles are healed when next
re-stored.
"""

import re

# An anchor whose visible text is empty and whose href targets "#Top" — the
# back-to-top link Flare/WebHelp injects.  e.g. [](...#Top) or [^](...#Top)
_BACK_TO_TOP_RE = re.compile(r"^\s*\[\^?\]\([^)]*#Top\)\s*$", re.IGNORECASE)

# A standalone "Was this article helpful?" / "Is this useful?" widget heading.
_HELPFUL_RE = re.compile(
    r"^\s*\**\s*(?:"
    # "Was this {page|article|doc|section|information} helpful/useful?"
    r"(?:was|is)\s+this\s+(?:\w+\s+)?(?:helpful|useful)"
    # "Did this answer your question?"
    r"|did\s+this\s+(?:\w+\s+)?answer\s+your\s+question"
    # "Did you find this/it helpful/useful?"
    r"|did\s+you\s+find\s+(?:this|it)\s+(?:\w+\s+)?(?:helpful|useful)"
    r")\s*\?*\s*\**\s*$",
    re.IGNORECASE,
)

# The answer/response lines that accompany the widget: "Yes No", thumbs, or the
# post-vote "Thanks for your feedback!" toast.
_YESNO_RE = re.compile(r"^\s*(?:yes\s*/?\s*no|yes|no|👍\s*👎)\s*$", re.IGNORECASE)
_FEEDBACK_THANKS_RE = re.compile(
    r"^\s*\**\s*thanks?\s+for\s+(?:your\s+)?feedback!?\s*\**\s*$", re.IGNORECASE
)

# Vote tallies help centres show: "12 out of 15 found this helpful",
# "3 people found this useful", "80% found this helpful".
_FOUND_HELPFUL_RE = re.compile(
    r"^\s*\**\s*\d+%?(?:\s+(?:out\s+)?of\s+\d+)?\s+(?:people\s+|users\s+)?"
    r"found\s+this\s+(?:article\s+|page\s+)?(?:helpful|useful)\b.*$",
    re.IGNORECASE,
)

# A standalone "Edit this page" / "Edit on GitHub" markdown link (Docusaurus,
# GitBook, mkdocs) — chrome, never documentation content.
_EDIT_LINK_RE = re.compile(
    r"^\s*\[[^\]]*\bedit\s+(?:this\s+)?(?:page|article|on\s+github)[^\]]*\]\(\S+\)\s*$",
    re.IGNORECASE,
)

# Copyright footer line (kept tail-anchored — see _strip_copyright_footer).
# An optional leading "|" handles landing pages where Flare renders the footer
# inside a markdown table cell ("| Copyright © … |") rather than as plain text.
_COPYRIGHT_RE = re.compile(r"^\s*\|?\s*Copyright\s*(?:©|\(c\))", re.IGNORECASE)

# Leading marketing banner signatures.
_PROMO_RE = re.compile(r"product innovations are live|\[Explore now\]\(", re.IGNORECASE)

# Intercom-hosted help centres (e.g. help.druva.com) prepend a font/Apache-
# license preamble to every page's first line: an Apache License notice for the
# embedded Lato font followed by its SIL Open Font License text, with the
# "[Skip to main content](…)" accessibility link glued onto the end. It is pure
# boilerplate and carries no documentation value. The signature requires both
# license names on the head line, so it can never match real prose.
_FONT_LICENSE_RE = re.compile(
    r"Apache License.*SIL Open Font License", re.IGNORECASE | re.DOTALL
)
_SKIP_TO_MAIN_RE = re.compile(r".*\[Skip to main content\]\([^)]*\)", re.IGNORECASE)

# GitBook leading "llms.txt / available as Markdown" banner that prefaces every
# page, e.g.:
#   For the complete documentation index, see [llms.txt](…/llms.txt)
#   . This page is also available as [Markdown](….md)
#   .
# Signature is the "complete documentation index … llms.txt" lead line.
_LLMS_BANNER_RE = re.compile(
    r"complete documentation index\b.*\bllms\.txt", re.IGNORECASE
)

# GitBook cookie/privacy-consent banner: the pitch line (mentions both cookies
# and the privacy policy) followed — after an optional wrapped "." and blanks —
# by the "Accept"/"Reject" buttons. Signature is specific enough never to match
# real prose. e.g.:
#   This site uses cookies to deliver its service and to analyze traffic. By
#   browsing this site, you accept the [privacy policy](…)
#   .
#   AcceptReject
_COOKIE_CONSENT_RE = re.compile(r"uses cookies\b.*\bprivacy policy", re.IGNORECASE)
_COOKIE_BUTTONS_RE = re.compile(
    r"^\s*\**\s*"
    r"\[?\s*Accept\s*\]?(?:\([^)]*\))?"   # "Accept" or "[Accept](…)"
    r"\s*/?\s*"                            # optional separator (none, space, "/")
    r"\[?\s*Reject\s*\]?(?:\([^)]*\))?"   # "Reject" or "[Reject](…)"
    r"\s*\**\s*$",
    re.IGNORECASE,
)

# GitBook prev/next page navigation: a standalone markdown link whose text is
# "Previous"/"Next" glued directly to the adjacent page title (no space), e.g.
#   [PreviousBackup & Archive - Overview](https://…/backup-and-archive)
#   [NextHow to Start a Free Trial](https://…/trial)
# The no-space-then-capital glue is the signature — a real link like
# "[Next steps](…)" has a space and is left untouched.
_PAGE_NAV_RE = re.compile(
    r"^\s*\[(?:Previous|Next)[A-Z][^\]]*\]\([^)]*\)\s*$"
)

# GitBook "Last updated N <unit> ago" footer line (optionally italicised with
# underscores/asterisks). The page's real last-updated timestamp is captured in
# article metadata, so this relative-time chrome is redundant noise.
# Trailing "[*_]*$" allows markdown emphasis right after "ago" (e.g. "…ago_"),
# where \b would fail since underscore counts as a word character.
_LAST_UPDATED_RE = re.compile(
    r"^\s*[*_]*\s*Last updated\b.*\bago[\s*_]*$", re.IGNORECASE
)

# A leading "You are here:" breadcrumb (e.g. Salesforce Help) sits above the title.
_BREADCRUMB_RE = re.compile(r"^\s*you are here\s*:?\s*$", re.IGNORECASE)
# A setext heading underline (=== or ---) marks the real title start.
_SETEXT_UNDERLINE_RE = re.compile(r"^\s*[=\-]{3,}\s*$")

# An ATX heading line: "# Title" … "###### Title" (optional closing #'s).
_ATX_HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*#*\s*$")

# A feedback-link table contains the JS feedback link or its known row texts.
_FEEDBACK_TABLE_SIG_RE = re.compile(
    r"SendLinkByMail|Provide feedback for the Documentation team",
    re.IGNORECASE,
)

_TABLE_LINE_RE = re.compile(r"^\s*\|")


def _norm(line: str) -> str:
    """Normalise NBSP and trailing whitespace for matching."""
    return line.replace("\xa0", " ").rstrip()


def _strip_back_to_top(lines: list[str]) -> list[str]:
    return [ln for ln in lines if not _BACK_TO_TOP_RE.match(_norm(ln))]


def _strip_helpful_widget(lines: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(lines):
        if _HELPFUL_RE.match(_norm(lines[i])):
            # Drop the heading, then a nearby Yes/No (or thanks-for-feedback)
            # answer line if present, skipping intervening blanks.
            j = i + 1
            while j < len(lines) and not _norm(lines[j]).strip():
                j += 1
            if j < len(lines) and (
                _YESNO_RE.match(_norm(lines[j]))
                or _FEEDBACK_THANKS_RE.match(_norm(lines[j]))
            ):
                i = j + 1
                continue
            i += 1
            continue
        out.append(lines[i])
        i += 1
    return out


def _strip_vote_tally(lines: list[str]) -> list[str]:
    """Drop standalone vote-tally / post-vote toast lines (full-line matches)."""
    return [
        ln for ln in lines
        if not (_FOUND_HELPFUL_RE.match(_norm(ln)) or _FEEDBACK_THANKS_RE.match(_norm(ln)))
    ]


def _strip_edit_links(lines: list[str]) -> list[str]:
    """Drop standalone 'Edit this page' / 'Edit on GitHub' chrome links."""
    return [ln for ln in lines if not _EDIT_LINK_RE.match(_norm(ln))]


def _strip_copyright_footer(lines: list[str]) -> list[str]:
    """Drop a trailing copyright footer block.

    Only fires when the ``Copyright ©`` line sits in the last few lines, so a
    mid-article mention of copyright is never treated as a footer.
    """
    tail_window = 10
    start = max(0, len(lines) - tail_window)
    for idx in range(len(lines) - 1, start - 1, -1):
        if _COPYRIGHT_RE.match(_norm(lines[idx])):
            cut = idx
            # When the footer is a markdown table ("| Copyright © … |"), also
            # drop the contiguous table scaffolding above it (the "|   |   |"
            # header and "| --- |" separator) so no empty table is left behind.
            # Stops at the first non-table line, so a real table separated by a
            # blank line is never consumed.
            while cut - 1 >= 0 and _TABLE_LINE_RE.match(lines[cut - 1]):
                cut -= 1
            return lines[:cut]
    return lines


def _strip_feedback_table(lines: list[str]) -> list[str]:
    """Remove a contiguous markdown table only if it carries a feedback signature."""
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if _TABLE_LINE_RE.match(lines[i]):
            j = i
            while j < n and _TABLE_LINE_RE.match(lines[j]):
                j += 1
            block = lines[i:j]
            if any(_FEEDBACK_TABLE_SIG_RE.search(b) for b in block):
                i = j  # drop the whole table block
                continue
            out.extend(block)
            i = j
            continue
        out.append(lines[i])
        i += 1
    return out


def _strip_lead_breadcrumb(lines: list[str]) -> list[str]:
    """Drop a leading 'You are here:' breadcrumb (+ its link list) before the title.

    Only fires when the document opens with the breadcrumb; everything up to the
    first heading (ATX ``#`` or a setext underline) is removed.
    """
    first = 0
    while first < len(lines) and not _norm(lines[first]).strip():
        first += 1
    if first >= len(lines) or not _BREADCRUMB_RE.match(_norm(lines[first])):
        return lines
    i = first + 1
    while i < len(lines):
        ln = _norm(lines[i])
        nxt = _norm(lines[i + 1]) if i + 1 < len(lines) else ""
        if ln.strip().startswith("#") or (ln.strip() and _SETEXT_UNDERLINE_RE.match(nxt)):
            break
        i += 1
    return lines[i:]


def _strip_cookie_consent(lines: list[str]) -> list[str]:
    """Drop a cookie/privacy-consent banner and its Accept/Reject buttons.

    Fires on the specific "uses cookies … privacy policy" signature, then also
    removes the trailing wrapped "." and the adjacent Accept/Reject line when
    present (skipping intervening blanks).
    """
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if _COOKIE_CONSENT_RE.search(_norm(lines[i])):
            j = i + 1
            # Skip blanks and a wrapped lone "." that follows the banner text.
            while j < n and (not _norm(lines[j]).strip() or _norm(lines[j]).strip() == "."):
                j += 1
            if j < n and _COOKIE_BUTTONS_RE.match(_norm(lines[j])):
                i = j + 1  # drop banner … buttons inclusive
                continue
            i += 1  # no buttons found — drop only the banner line
            continue
        out.append(lines[i])
        i += 1
    return out


def _strip_last_updated(lines: list[str]) -> list[str]:
    """Drop standalone 'Last updated N <unit> ago' footer lines (GitBook)."""
    return [ln for ln in lines if not _LAST_UPDATED_RE.match(_norm(ln))]


def _strip_page_nav(lines: list[str]) -> list[str]:
    """Drop standalone GitBook prev/next page-navigation links."""
    return [ln for ln in lines if not _PAGE_NAV_RE.match(_norm(ln))]


def _strip_lead_llms_banner(lines: list[str]) -> list[str]:
    """Drop GitBook's leading 'llms.txt / available as Markdown' banner.

    Only fires when the document opens with the banner; removes its contiguous
    (non-blank) paragraph — the lead line, the wrapped 'available as Markdown'
    line, and a trailing lone '.' — up to the first blank line.
    """
    first = 0
    while first < len(lines) and not _norm(lines[first]).strip():
        first += 1
    if first >= len(lines) or not _LLMS_BANNER_RE.search(_norm(lines[first])):
        return lines
    j = first
    while j < len(lines) and _norm(lines[j]).strip():
        j += 1
    return lines[j:]


def _strip_lead_font_license(lines: list[str]) -> list[str]:
    """Drop an Intercom font/Apache-license preamble from the document head.

    Only fires when the first non-blank line carries the license signature. The
    preamble ends with a glued "[Skip to main content](…)" link; everything up
    to and including that link is removed, preserving any real content that
    happens to follow it on the same line. If the link is absent, the whole
    matched line is dropped.
    """
    first = 0
    while first < len(lines) and not _norm(lines[first]).strip():
        first += 1
    if first >= len(lines) or not _FONT_LICENSE_RE.search(lines[first]):
        return lines
    m = _SKIP_TO_MAIN_RE.match(lines[first])
    remainder = lines[first][m.end():] if m else ""
    out = lines[:first]
    if remainder.strip():
        out.append(remainder)
    out.extend(lines[first + 1:])
    return out


def _strip_lead_promo_banner(lines: list[str]) -> list[str]:
    """Drop a leading marketing banner (and a lone trailing '.') before the title."""
    # Find first non-blank line.
    first = 0
    while first < len(lines) and not _norm(lines[first]).strip():
        first += 1
    if first >= len(lines) or not _PROMO_RE.search(lines[first]):
        return lines
    drop_to = first + 1
    # A banner often wraps so the URL's trailing "." lands on its own line.
    if drop_to < len(lines) and _norm(lines[drop_to]).strip() == ".":
        drop_to += 1
    while drop_to < len(lines) and not _norm(lines[drop_to]).strip():
        drop_to += 1
    return lines[drop_to:]


def _heading_at(lines: list[str], i: int) -> tuple[str, int, str, int] | None:
    """Return ``(style, level, text, span)`` for a heading starting at line *i*,
    else None.

    ``style`` is ``"atx"`` (span 1, e.g. ``## X``) or ``"setext"`` (span 2: a
    text line followed by a ``===``/``---`` underline).
    """
    norm = _norm(lines[i])
    m = _ATX_HEADING_RE.match(norm)
    if m:
        return ("atx", len(m.group(1)), m.group(2).strip(), 1)
    if (
        norm.strip()
        and i + 1 < len(lines)
        and _SETEXT_UNDERLINE_RE.match(_norm(lines[i + 1]))
    ):
        level = 1 if _norm(lines[i + 1]).strip()[0] == "=" else 2
        return ("setext", level, norm.strip(), 2)
    return None


def _strip_duplicate_title(lines: list[str]) -> list[str]:
    """Drop later headings that exactly repeat the document's first (title) heading.

    Some templates wrap one article in several responsive blocks that each
    re-emit the H1 (e.g. Keepit's two ``<article>`` blocks), so the title shows
    up two+ times. Only an exact match of the *first* heading — same style,
    level, and text — is removed, so legitimately repeated section headings are
    left untouched.
    """
    idx = 0
    title = None
    while idx < len(lines):
        title = _heading_at(lines, idx)
        if title:
            break
        idx += 1
    if not title or not title[2]:
        return lines
    style, level, text, span = title
    out = lines[: idx + span]
    i = idx + span
    while i < len(lines):
        h = _heading_at(lines, i)
        if h and (h[0], h[1], h[2]) == (style, level, text):
            i += h[3]  # skip the duplicate heading's line(s)
            continue
        out.append(lines[i])
        i += 1
    return out


_RULES = (
    _strip_lead_font_license,
    _strip_lead_breadcrumb,
    _strip_lead_llms_banner,
    _strip_lead_promo_banner,
    _strip_duplicate_title,
    _strip_cookie_consent,
    _strip_feedback_table,
    _strip_helpful_widget,
    _strip_vote_tally,
    _strip_edit_links,
    _strip_back_to_top,
    _strip_page_nav,
    _strip_last_updated,
    _strip_copyright_footer,
)


def sanitize_markdown(md: str) -> str:
    """Return ``md`` with recurring boilerplate removed (or ``md`` unchanged if
    the pass would remove too much — see module docstring)."""
    if not md or not md.strip():
        return md

    lines = md.split("\n")
    for rule in _RULES:
        lines = rule(lines)

    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    if not cleaned:
        return md  # a rule misfired and emptied the article — keep the original
    return cleaned + "\n"
