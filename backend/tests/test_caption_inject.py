import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.image_describe import inject_caption

URL = "/media/abc/x.png"


def test_inserts_caption_after_image():
    md = f"# Title\n\n![topology]({URL})\n\nBody text."
    out = inject_caption(md, URL, "A topology diagram.")
    assert f"![topology]({URL})" in out
    assert "> **Figure:** A topology diagram." in out
    # Caption sits after the image, before the body.
    assert out.index("> **Figure:**") > out.index(URL)
    assert out.index("> **Figure:**") < out.index("Body text.")


def test_idempotent_same_description():
    md = f"![t]({URL})\n\nBody."
    once = inject_caption(md, URL, "Desc one.")
    twice = inject_caption(once, URL, "Desc one.")
    assert once == twice


def test_replaces_existing_caption():
    md = f"![t]({URL})\n\nBody."
    first = inject_caption(md, URL, "Old description.")
    second = inject_caption(first, URL, "New description.")
    assert "New description." in second
    assert "Old description." not in second
    assert second.count("> **Figure:**") == 1


def test_missing_url_unchanged():
    md = "No image here.\n"
    assert inject_caption(md, URL, "Desc.") == md


def test_inline_image_caption_sits_directly_under_image():
    # Image inline in a paragraph: prose before AND after the ![](…) on one line.
    md = f"Intro text before. ![alt]({URL})Trailing prose after the image."
    out = inject_caption(md, URL, "A diagram.")
    lines = out.split("\n")
    img_i = next(i for i, ln in enumerate(lines) if f"]({URL})" in ln)
    cap_i = next(i for i, ln in enumerate(lines) if "> **Figure:**" in ln)
    # Image is alone on its line, caption is the next non-blank line, and the
    # trailing prose is pushed BELOW the caption (not between image and caption).
    assert lines[img_i].strip() == f"![alt]({URL})"
    assert cap_i == img_i + 2 and lines[img_i + 1] == ""
    assert out.index("Trailing prose") > out.index("> **Figure:**")
    assert out.index("Intro text before.") < out.index(f"]({URL})")


def test_inline_image_idempotent():
    md = f"Before. ![alt]({URL})After."
    once = inject_caption(md, URL, "Desc.")
    twice = inject_caption(once, URL, "Desc.")
    assert once == twice
    assert once.count("> **Figure:**") == 1


# ── Titled image references ──────────────────────────────────────────────────
# markdownify renders <img title="…"> as ![alt](src "title"); AvePoint, Securiti
# and others set title on every screenshot. The old "](url)" needle never matched
# those, so the description was stored (and counted as done) while no caption ever
# reached the content.

def test_caption_injected_for_titled_image_reference():
    md = f'# Title\n\n![Rapid recovery jobs]({URL} "Rapid recovery jobs.")\n\nBody text.'
    out = inject_caption(md, URL, "Screenshot of the rapid recovery jobs screen.")
    assert "> **Figure:** Screenshot of the rapid recovery jobs screen." in out
    # The title is preserved — only a caption is added.
    assert f'![Rapid recovery jobs]({URL} "Rapid recovery jobs.")' in out
    assert out.index("> **Figure:**") > out.index(URL)
    assert out.index("> **Figure:**") < out.index("Body text.")


def test_titled_image_idempotent_and_refreshable():
    md = f'![t]({URL} "A title")\n\nBody.'
    once = inject_caption(md, URL, "Desc one.")
    assert inject_caption(once, URL, "Desc one.") == once
    twice = inject_caption(once, URL, "Desc two.")
    assert "Desc two." in twice and "Desc one." not in twice
    assert twice.count("> **Figure:**") == 1


def test_titled_inline_image_is_isolated():
    md = f'Intro. ![alt]({URL} \'single quoted\')Trailing prose.'
    out = inject_caption(md, URL, "A diagram.")
    lines = out.split("\n")
    img_i = next(i for i, ln in enumerate(lines) if URL in ln)
    assert lines[img_i].strip() == f"![alt]({URL} 'single quoted')"
    assert lines[img_i + 2] == "> **Figure:** A diagram."
    assert out.index("Trailing prose") > out.index("> **Figure:**")


def test_paren_quoted_title_and_angle_bracket_url():
    for ref in (f"![a]({URL} (paren title))", f"![a](<{URL}>)"):
        out = inject_caption(f"{ref}\n\nBody.", URL, "Desc.")
        assert "> **Figure:** Desc." in out, ref
        assert ref in out, ref


def test_titled_image_with_escaped_quote_in_title():
    # markdownify escapes a double quote embedded in the source <img title="…">
    # as \" — a bare [^"]* alternative stops at that first escaped quote, well
    # short of the real closing quote, so the whole token never matches (same
    # silent-skip failure the titled-reference fix above addresses).
    title = 'Error: \\"Entity is unavailable\\" shown to the user'
    md = f'![alt]({URL} "{title}")\n\nBody text.'
    out = inject_caption(md, URL, "A screenshot of the error dialog.")
    assert "> **Figure:** A screenshot of the error dialog." in out
    assert f'![alt]({URL} "{title}")' in out
    assert out.index("> **Figure:**") > out.index(URL)


def test_longer_path_with_same_prefix_is_not_matched():
    # A different image whose path merely starts with URL must not be captioned.
    md = f"![other]({URL}.thumb.png)\n\nBody."
    assert inject_caption(md, URL, "Desc.") == md
