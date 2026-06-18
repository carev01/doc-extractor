"""Render export markdown to a self-contained PDF via WeasyPrint."""

import markdown as _markdown
from weasyprint import HTML

# Minimal print stylesheet — readable body, page margins, sensible code/tables.
_CSS = """
@page { size: A4; margin: 2cm 1.8cm; }
body { font-family: "DejaVu Sans", sans-serif; font-size: 11pt; line-height: 1.5; color: #1a1a1a; }
h1 { font-size: 20pt; } h2 { font-size: 15pt; border-bottom: 1px solid #ccc; padding-bottom: 2px; }
h3 { font-size: 12.5pt; }
a { color: #0b66c2; text-decoration: none; }
code, pre { font-family: "DejaVu Sans Mono", monospace; font-size: 9.5pt; }
pre { background: #f4f4f4; padding: 8px; border-radius: 4px; white-space: pre-wrap; word-wrap: break-word; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #ccc; padding: 4px 8px; text-align: left; }
img { max-width: 100%; }
hr { border: none; border-top: 1px solid #ddd; margin: 16px 0; }
"""

_EXTENSIONS = ["tables", "fenced_code", "toc"]


def render_markdown_to_pdf(markdown_text: str, base_url: str) -> bytes:
    """Convert export markdown to PDF bytes.

    Relative image URLs in the markdown resolve against ``base_url`` (the
    canonical media directory), and WeasyPrint embeds them into the PDF, so the
    result is self-contained. A missing image is skipped by WeasyPrint rather
    than raising, matching the markdown export's tolerance.
    """
    body_html = _markdown.markdown(markdown_text, extensions=_EXTENSIONS)
    document = (
        f"<html><head><meta charset='utf-8'><style>{_CSS}</style></head>"
        f"<body>{body_html}</body></html>"
    )
    return HTML(string=document, base_url=base_url).write_pdf()
