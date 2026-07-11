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
