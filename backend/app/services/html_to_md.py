"""HTML → Markdown conversion with GFM-table normalisation.

``markdownify`` only emits a GFM header-separator row (``| --- | --- |``) when the
header ``<tr>`` has no previous sibling. Many doc platforms put a ``<caption>``
(or ``<colgroup>``) before the first row, or never wrap the header in
``<thead>`` — so markdownify renders the header row but omits the separator, and
*no* Markdown renderer then shows a table (it degrades to literal pipe text).
Veeam Help Center tables (``<table><caption>…</caption><tr><th>…``) hit this.

``html_to_markdown`` normalises tables first:
- a ``<caption>`` is lifted to a bold paragraph just before the table (the title
  is kept and the first row becomes header-detectable);
- a header row (first ``<tr>`` whose cells are ``<th>``) is wrapped in ``<thead>``
  so the separator is always emitted, regardless of what precedes it.

Content with no ``<table>`` is passed through untouched (byte-identical to a plain
``markdownify`` call), so non-table articles don't churn. GFM tables can't express
``rowspan``/``colspan``; markdownify still flattens those, which is the best a
Markdown table can do.
"""

from bs4 import BeautifulSoup
from markdownify import markdownify


def _normalize_tables(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        # Lift the caption out to a bold paragraph above the table.
        cap = table.find("caption", recursive=False)
        if cap is not None:
            text = cap.get_text(" ", strip=True)
            cap.decompose()
            if text:
                p = soup.new_tag("p")
                strong = soup.new_tag("strong")
                strong.string = text
                p.append(strong)
                table.insert_before(p)
        # Ensure the header row sits in <thead> so the GFM separator is emitted.
        if table.find("thead") is None:
            first_tr = table.find("tr")
            if first_tr is not None and first_tr.find("th", recursive=False) is not None:
                thead = soup.new_tag("thead")
                first_tr.insert_before(thead)
                thead.append(first_tr.extract())
    return str(soup)


def html_to_markdown(html: str) -> str:
    """Convert ``html`` to Markdown, normalising any tables to valid GFM first."""
    if not html:
        return ""
    if "<table" in html.lower():
        html = _normalize_tables(html)
    return markdownify(html).strip()
