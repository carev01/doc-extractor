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
