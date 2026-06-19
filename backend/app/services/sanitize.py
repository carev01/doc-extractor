"""Post-process sanitisation of scraped article markdown.

Removes recurring site chrome/boilerplate that adds no documentation value —
"Was this article helpful?" feedback widgets, back-to-top anchors, copyright
footers, feedback-link tables, leading marketing banners — while staying
conservative:

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
    r"^\s*\**\s*(?:was|is)\s+this\s+(?:article\s+)?(?:helpful|useful)\s*\?*\s*\**\s*$",
    re.IGNORECASE,
)

# The Yes/No answer line that follows the widget (NBSP-separated in Flare).
_YESNO_RE = re.compile(r"^\s*(?:yes\s*no|yes|no)\s*$", re.IGNORECASE)

# Copyright footer line (kept tail-anchored — see _strip_copyright_footer).
# An optional leading "|" handles landing pages where Flare renders the footer
# inside a markdown table cell ("| Copyright © … |") rather than as plain text.
_COPYRIGHT_RE = re.compile(r"^\s*\|?\s*Copyright\s*(?:©|\(c\))", re.IGNORECASE)

# Leading marketing banner signatures.
_PROMO_RE = re.compile(r"product innovations are live|\[Explore now\]\(", re.IGNORECASE)

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
            # Drop the heading, then a nearby Yes/No answer line (skipping blanks).
            j = i + 1
            while j < len(lines) and not _norm(lines[j]).strip():
                j += 1
            if j < len(lines) and _YESNO_RE.match(_norm(lines[j])):
                i = j + 1
                continue
            i += 1
            continue
        out.append(lines[i])
        i += 1
    return out


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


_RULES = (
    _strip_lead_promo_banner,
    _strip_feedback_table,
    _strip_helpful_widget,
    _strip_back_to_top,
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
