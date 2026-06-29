import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.firecrawl import _extract_fragment


def test_extract_fragment_returns_outer_html():
    html = '<html><body><nav id="wh_publication_toc"><ul><li>x</li></ul></nav><article>a</article></body></html>'
    frag = _extract_fragment(html, "#wh_publication_toc")
    assert frag is not None
    assert 'id="wh_publication_toc"' in frag and "<li>x</li>" in frag
    assert "<article>" not in frag


def test_extract_fragment_none_when_absent():
    assert _extract_fragment("<html><body><p>x</p></body></html>", "#wh_publication_toc") is None
