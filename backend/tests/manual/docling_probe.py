# backend/tests/manual/docling_probe.py
"""Manual probe: confirm Docling's heading/table/provenance API on this version.

Run:  SCRATCH=<scratchpad dir> python tests/manual/docling_probe.py
Prints the engine surface Tasks 4-5 depend on so the field/enum names can be
verified against the installed docling version.
"""
import os
import sys
from io import BytesIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.base_models import DocumentStream, InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions

pdf = os.path.join(os.environ["SCRATCH"], "HYCU_CompatibilityMatrix.pdf")
data = open(pdf, "rb").read()

# Disable OCR so the probe works on native PDFs without downloading OCR models
_opts = PdfPipelineOptions()
_opts.do_ocr = False
conv = DocumentConverter(
    format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=_opts)}
)
res = conv.convert(DocumentStream(name="probe.pdf", stream=BytesIO(data)))
doc = res.document

md = doc.export_to_markdown()
print("=== markdown length:", len(md))
print(md[:600])
print("\n=== iterate_items labels/levels/pages ===")
for item, level in doc.iterate_items():
    label = getattr(item, "label", None)
    prov = getattr(item, "prov", None) or []
    page = prov[0].page_no if prov else None
    text = (getattr(item, "text", "") or "")[:50]
    print(repr(str(label)), "lvl=", getattr(item, "level", None),
          "tree_level=", level, "page=", page, "text=", repr(text))
print("\n=== tables ===", len(getattr(doc, "tables", []) or []))
for t in (getattr(doc, "tables", []) or []):
    prov = getattr(t, "prov", None) or []
    print("table page=", prov[0].page_no if prov else None)
