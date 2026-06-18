# PDF Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add PDF as an export format alongside markdown, reusing all selection/splitting logic and rendering each article-group to a self-contained PDF (images embedded) delivered through the existing ZIP + download route.

**Architecture:** A new `pdf_renderer` module turns a group's markdown into PDF bytes via `markdown`→HTML→WeasyPrint. `ExportRequest` gains `format`; `export`/`export_sync`/`_generate_export` thread it through and branch only at the per-group render/packaging step. The selection, splitting, and "never split an article" grouping are untouched.

**Tech Stack:** FastAPI, SQLAlchemy, WeasyPrint 69, Python `markdown` 3.10; React + TypeScript frontend.

## Global Constraints

- Pin `markdown==3.10.2` and `weasyprint==69.0` in `requirements.txt`. Both import and render a real PDF on the host test env (verified), so unit tests run normally — no import-skip needed.
- The backend `Dockerfile` (base `python:3.13-slim`) must `apt-get install` WeasyPrint's native libs before `pip install`.
- PDF honours the same `split_by`/`respect_chapters` grouping as markdown; a single article is never split across files (existing invariant, unchanged).
- PDF bundles contain only `.pdf` file(s) — images are embedded, no `images/` dir. The markdown path is unchanged.
- Packaging and download are unchanged: always a ZIP via `GET /api/export/download/{export_id}`.
- Backend export-engine tests use the synchronous `db_session` fixture + `ExportEngine().export_sync(...)` (see `tests/test_integration.py`); route tests use the async client. Run from `backend/` with `pytest`.
- Frontend verified via `npm run build` + `npm run lint`; introduce no new lint errors.
- Branch: `feat/pdf-export` (off the merged `main`). Interpreter is `python3`.

---

### Task 1: Dependencies (Python + Docker native libs)

**Files:**
- Modify: `backend/requirements.txt`
- Modify: `backend/Dockerfile`

**Interfaces:**
- Produces: `markdown` and `weasyprint` importable in the backend (host + image).

- [ ] **Step 1: Add the Python deps**

Append to `backend/requirements.txt`:
```
markdown==3.10.2
weasyprint==69.0
```
Run: `cd backend && pip3 install --break-system-packages markdown==3.10.2 weasyprint==69.0`
Expected: both install (may report "already satisfied").

- [ ] **Step 2: Verify host import + render**

Run:
```bash
python3 -c "import markdown, weasyprint; html=markdown.markdown('# Hi'); print(weasyprint.HTML(string=html, base_url='/tmp/').write_pdf()[:5])"
```
Expected: prints `b'%PDF-'`.

- [ ] **Step 3: Add native libs to the Dockerfile**

In `backend/Dockerfile`, replace the dependency-install block so the apt libs WeasyPrint needs are present before `pip install`:

```dockerfile
FROM python:3.13-slim

WORKDIR /app

# WeasyPrint native dependencies (Pango + fonts). Pillow (pip) handles image
# decoding, so no extra image libs are required.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangoft2-1.0-0 libfontconfig1 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Run migrations then start the server.
# For Kubernetes, replace this with an init container for the migration step
# so only one pod runs `alembic upgrade head` per deploy.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
```

(The image build itself is verified live in Task 5.)

- [ ] **Step 4: Commit**

```bash
git add backend/requirements.txt backend/Dockerfile
git commit -m "build: add weasyprint + markdown and their native libs for PDF export"
```

---

### Task 2: `pdf_renderer` module

**Files:**
- Create: `backend/app/services/pdf_renderer.py`
- Test: `backend/tests/test_pdf_renderer.py`

**Interfaces:**
- Produces: `render_markdown_to_pdf(markdown_text: str, base_url: str) -> bytes`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_pdf_renderer.py`:

```python
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.pdf_renderer import render_markdown_to_pdf


def test_renders_pdf_bytes():
    pdf = render_markdown_to_pdf("# Title\n\nSome **bold** text.", base_url="/tmp/")
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 500


def test_renders_tables_and_code():
    md = "| a | b |\n|---|---|\n| 1 | 2 |\n\n```\ncode\n```\n"
    pdf = render_markdown_to_pdf(md, base_url="/tmp/")
    assert pdf[:5] == b"%PDF-"


def test_embeds_local_image(tmp_path):
    # A 1x1 PNG written to base_url; referencing it should grow the PDF.
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108020000"
        "00907753de0000000c4944415408d763f8cfc0f01f0005000155a3"
        "0a0a0000000049454e44ae426082"
    )
    (tmp_path / "img.png").write_bytes(png)
    base = str(tmp_path) + os.sep
    without = render_markdown_to_pdf("# No image", base_url=base)
    with_img = render_markdown_to_pdf("# Image\n\n![x](img.png)", base_url=base)
    assert with_img[:5] == b"%PDF-"
    assert len(with_img) > len(without)


def test_missing_image_does_not_raise(tmp_path):
    base = str(tmp_path) + os.sep
    pdf = render_markdown_to_pdf("# Doc\n\n![gone](does-not-exist.png)", base_url=base)
    assert pdf[:5] == b"%PDF-"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && pytest tests/test_pdf_renderer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.pdf_renderer'`.

- [ ] **Step 3: Implement the renderer**

Create `backend/app/services/pdf_renderer.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && pytest tests/test_pdf_renderer.py -v`
Expected: all 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/pdf_renderer.py backend/tests/test_pdf_renderer.py
git commit -m "feat(export): markdown-to-PDF renderer (WeasyPrint, embedded images)"
```

---

### Task 3: Thread `format` through the export engine + route

**Files:**
- Modify: `backend/app/schemas/export.py`
- Modify: `backend/app/services/exporter.py`
- Modify: `backend/app/routes/export.py`
- Test: `backend/tests/test_integration.py`

**Interfaces:**
- Consumes: `render_markdown_to_pdf(markdown_text, base_url)` (Task 2).
- Produces: `ExportRequest.format: str = "markdown"`; `export(..., format="markdown")` and `export_sync(..., format="markdown")`; `_generate_export(..., format="markdown")`. PDF bundles contain `.pdf` files.

- [ ] **Step 1: Add `format` to the request schema**

In `backend/app/schemas/export.py`, add to `ExportRequest` (after `respect_chapters`):
```python
    # Output format. "pdf" renders each group to a self-contained PDF.
    format: str = "markdown"  # "markdown" | "pdf"
```

- [ ] **Step 2: Write the failing test**

Add to `backend/tests/test_integration.py`:
```python
def test_export_pdf_full(db_session):
    v = Vendor(name="PdfVendor")
    db_session.add(v)
    db_session.flush()
    s = DocumentationSource(vendor_id=v.id, name="PdfSource", base_url="https://docs.pdf.com")
    db_session.add(s)
    db_session.flush()
    for i in range(3):
        db_session.add(Article(
            source_id=s.id, title=f"Article {i}",
            source_url=f"https://docs.pdf.com/{i}",
            content_markdown=f"# Article {i}\n\nContent {i}.",
            sort_order=i, estimated_tokens=50, content_size_bytes=200,
        ))
    db_session.commit()

    engine = ExportEngine()
    result = engine.export_sync(db_session, source_id=s.id, format="pdf")
    assert result["file_count"] == 1
    export_dir = os.path.join(engine.export_dir, str(result["export_id"]))
    pdf_files = [f for f in os.listdir(export_dir) if f.endswith(".pdf")]
    assert len(pdf_files) == 1
    # Self-contained: a PDF bundle has no images/ directory.
    assert not os.path.isdir(os.path.join(export_dir, "images"))
    with open(os.path.join(export_dir, pdf_files[0]), "rb") as f:
        assert f.read(5) == b"%PDF-"
    assert result["files"][0]["filename"].endswith(".pdf")


def test_export_pdf_split_produces_multiple_pdfs(db_session):
    v = Vendor(name="PdfSplitVendor")
    db_session.add(v)
    db_session.flush()
    s = DocumentationSource(vendor_id=v.id, name="PdfSplit", base_url="https://docs.ps.com")
    db_session.add(s)
    db_session.flush()
    for i in range(4):
        db_session.add(Article(
            source_id=s.id, title=f"A{i}", source_url=f"https://docs.ps.com/{i}",
            content_markdown=f"# A{i}\n\nx", sort_order=i,
            estimated_tokens=50, content_size_bytes=200,
        ))
    db_session.commit()

    engine = ExportEngine()
    result = engine.export_sync(
        db_session, source_id=s.id, split_by="articles",
        max_articles_per_file=2, format="pdf",
    )
    assert result["file_count"] == 2
    export_dir = os.path.join(engine.export_dir, str(result["export_id"]))
    pdf_files = [f for f in os.listdir(export_dir) if f.endswith(".pdf")]
    assert len(pdf_files) == 2
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd backend && pytest tests/test_integration.py -k export_pdf -v`
Expected: FAIL — `export_sync()` got an unexpected keyword argument `format`.

- [ ] **Step 4: Thread `format` through `export` and `export_sync`**

In `backend/app/services/exporter.py`, add a `format: str = "markdown"` parameter to BOTH `async def export(` and `def export_sync(` (place it last, after `respect_chapters: bool = False,`), and pass it into the `_generate_export` call in each method. Each call currently ends:
```python
        return self._generate_export(
            articles, source.name, source_id, split_by,
            max_articles_per_file, max_file_size_bytes, max_tokens_per_file,
            respect_chapters, chapter_keys,
        )
```
Change BOTH (in `export` and `export_sync`) to append `format`:
```python
        return self._generate_export(
            articles, source.name, source_id, split_by,
            max_articles_per_file, max_file_size_bytes, max_tokens_per_file,
            respect_chapters, chapter_keys, format,
        )
```

- [ ] **Step 5: Branch `_generate_export` on format**

In `backend/app/services/exporter.py`, update `_generate_export`'s signature to accept `format`:
```python
    def _generate_export(
        self,
        articles: list[Article],
        source_name: str,
        source_id: uuid.UUID,
        split_by: str | None = None,
        max_articles_per_file: int | None = None,
        max_file_size_bytes: int | None = None,
        max_tokens_per_file: int | None = None,
        respect_chapters: bool = False,
        chapter_keys: dict[uuid.UUID, uuid.UUID | None] | None = None,
        format: str = "markdown",
    ) -> dict:
```
Add the import near the top of the file (with the other `app.services`/stdlib imports):
```python
from app.services.pdf_renderer import render_markdown_to_pdf
```
Replace the per-group loop body so it branches on `format` (the markdown branch is the existing behaviour, unchanged):
```python
        for i, group in enumerate(groups, 1):
            base_name = source_name.replace(" ", "_")
            ext = "pdf" if format == "pdf" else "md"
            if len(groups) == 1:
                filename = f"{base_name}.{ext}"
            else:
                filename = f"{base_name}_part{i:03d}.{ext}"

            markdown_doc = self._build_markdown_document(group, source_name)
            filepath = os.path.join(export_subdir, filename)

            if format == "pdf":
                # Resolve /media/<id>/<file> against the media root so WeasyPrint
                # embeds the images directly into the PDF (self-contained).
                pdf_md = markdown_doc.replace(f"{settings.media_url_prefix}/", "")
                pdf_bytes = render_markdown_to_pdf(
                    pdf_md, base_url=self.media_root + os.sep
                )
                with open(filepath, "wb") as f:
                    f.write(pdf_bytes)
                file_size = len(pdf_bytes)
            else:
                # Rewrite served media URLs to bundle-relative paths for offline MD.
                content = markdown_doc.replace(
                    f"{settings.media_url_prefix}/", "images/"
                )
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)
                file_size = len(content.encode("utf-8"))

            archive_members.append((filepath, filename))
            total_size += file_size
            group_tokens = sum(a.estimated_tokens for a in group)

            files_info.append({
                "filename": filename,
                "article_count": len(group),
                "size_bytes": file_size,
                "estimated_tokens": group_tokens,
                "first_article_title": group[0].title,
                "last_article_title": group[-1].title,
            })
```
Then guard the image-copy loop so it only runs for markdown (PDF embeds images). Wrap the existing `copied: set[str] = set()` / `for article in articles:` image-copy block in:
```python
        if format != "pdf":
            copied: set[str] = set()
            for article in articles:
                for image in article.images:
                    ...  # (existing copy logic unchanged)
```
Leave the zip-bundling and the returned dict unchanged. (`zip_filename` stays `<base>.zip`.)

- [ ] **Step 6: Pass `format` from the route + include `.pdf` in `list_exports`**

In `backend/app/routes/export.py`, add `format=body.format,` to the `export_engine.export(...)` call in `export_markdown`. In `list_exports`, change the file filter from `f.name.endswith(".md")` to `f.name.endswith((".md", ".pdf"))`.

- [ ] **Step 7: Run the PDF tests + the full export suite**

Run: `cd backend && pytest tests/test_integration.py -v`
Expected: the two new `export_pdf` tests PASS and all existing export tests still PASS (markdown unaffected).

- [ ] **Step 8: Commit**

```bash
git add backend/app/schemas/export.py backend/app/services/exporter.py backend/app/routes/export.py backend/tests/test_integration.py
git commit -m "feat(export): render PDF bundles when format=pdf"
```

---

### Task 4: Frontend format toggle

**Files:**
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/components/ExportPanel.tsx`

**Interfaces:**
- Consumes: `POST /api/export/markdown` with `format` (Task 3); existing `exportMarkdown(data: ExportRequest)`.
- Produces: a Format (Markdown/PDF) control that sets `format` on the request.

- [ ] **Step 1: Add `format` to the TS request type**

In `frontend/src/types/index.ts`, add to `ExportRequest` (after `respect_chapters?`):
```typescript
  format?: "markdown" | "pdf";
```

- [ ] **Step 2: Add format state + control + request field in `ExportPanel`**

In `frontend/src/components/ExportPanel.tsx`:

Add state near the other `useState` hooks (after `respectChapters`):
```tsx
  const [format, setFormat] = useState<"markdown" | "pdf">("markdown");
```

Add `format` to the `exportMarkdown({ ... })` request object (alongside `split_by` / `respect_chapters`):
```tsx
        format,
```

Render a Format toggle near the split controls (match the existing control styling, e.g. the `.export-mode` / segmented-button pattern already in the panel):
```tsx
      <div className="export-format">
        <label>Format</label>
        <div className="seg">
          <button
            type="button"
            className={format === "markdown" ? "active" : ""}
            onClick={() => setFormat("markdown")}
          >
            Markdown
          </button>
          <button
            type="button"
            className={format === "pdf" ? "active" : ""}
            onClick={() => setFormat("pdf")}
          >
            PDF
          </button>
        </div>
      </div>
```
(If the panel already has a segmented-control class for `mode`, reuse it instead of `.seg` and skip new CSS. Otherwise add minimal `.export-format` / `.seg` rules to `App.css` matching the existing button-group styling — the split controls are styled around `.export-mode`.)

- [ ] **Step 3: Build + lint**

Run: `cd frontend && npm run build && npm run lint`
Expected: build succeeds, no type errors; no new lint errors vs the baseline.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/components/ExportPanel.tsx frontend/src/App.css
git commit -m "feat(ui): export format toggle (Markdown / PDF)"
```

---

### Task 5: Live container verification

**Files:** none (verification only).

- [ ] **Step 1: Rebuild the backend image (with native libs) + frontend**

Run: `docker compose up -d --build backend frontend`
Expected: the backend image builds (apt installs the Pango libs, pip installs weasyprint) and starts; no crash loop in `docker compose logs backend | tail`.

- [ ] **Step 2: Export the Clumio source as PDF via the API**

Run (substitute the Clumio source id):
```bash
SID=$(curl -s http://localhost:8000/api/sources | python3 -c "import sys,json;print(json.load(sys.stdin)['sources'][0]['id'])")
EXPORT=$(curl -s -X POST http://localhost:8000/api/export/markdown -H 'Content-Type: application/json' -d "{\"source_id\":\"$SID\",\"format\":\"pdf\"}")
echo "$EXPORT" | python3 -c "import sys,json;d=json.load(sys.stdin);print('files:',[f['filename'] for f in d['files']]);print('zip:',d['zip_filename'])"
```
Expected: `files` are `.pdf`; a `zip_filename` is returned.

- [ ] **Step 3: Download the ZIP and confirm it contains a valid PDF**

Run:
```bash
EID=$(echo "$EXPORT" | python3 -c "import sys,json;print(json.load(sys.stdin)['export_id'])")
curl -s "http://localhost:8000/api/export/download/$EID" -o /tmp/clumio_pdf.zip
python3 -c "import zipfile;z=zipfile.ZipFile('/tmp/clumio_pdf.zip');print(z.namelist());n=[x for x in z.namelist() if x.endswith('.pdf')][0];print('pdf magic:', z.read(n)[:5])"
```
Expected: the ZIP lists `.pdf` file(s) and no `images/` entries; the PDF starts with `b'%PDF-'`.

- [ ] **Step 4: Spot-check images are embedded**

Pick an article known to have images (Clumio has ~106). Confirm its PDF is substantially larger than a text-only export, or open `/tmp/clumio_pdf.zip` and inspect the PDF visually if a viewer is available. Expected: images render inline (no broken-image boxes).

---

## Self-Review

**Spec coverage:**
- `format` flag on request + threaded through export/export_sync/_generate_export → Tasks 1(schema in 3), 3.
- `pdf_renderer` (markdown→HTML→WeasyPrint, print CSS, embedded images, missing-image tolerance) → Task 2.
- PDF honours split grouping; one PDF per group; never splits an article → Task 3 (reuses existing grouping; `test_export_pdf_split_produces_multiple_pdfs`).
- Images embedded; no `images/` dir for PDF → Task 3 (image-copy guarded; test asserts no `images/`).
- Always-ZIP packaging + unchanged download route → Task 3 (zip block unchanged); `list_exports` `.pdf` filter.
- Dependencies (weasyprint + markdown) + Dockerfile native libs → Task 1.
- Frontend Format toggle, split controls apply to both → Task 4.
- Testing: renderer unit (Task 2), exporter pdf + markdown regression (Task 3), live container (Task 5), frontend build/lint (Task 4).

**Placeholder scan:** No TBD/TODO. Two intentionally directed (not vague) steps: Task 4 Step 2 says "reuse the existing segmented-control class if present, else add minimal CSS" because the exact class name isn't known without reading `ExportPanel`; and Task 5 Step 4 is a visual spot-check. Both give concrete actions.

**Type consistency:** `format` is `str = "markdown"` everywhere in Python (schema, `export`, `export_sync`, `_generate_export`) and `"markdown" | "pdf"` in TS; `render_markdown_to_pdf(markdown_text: str, base_url: str) -> bytes` is defined in Task 2 and called identically in Task 3 with `base_url=self.media_root + os.sep`. `self.media_root` is the existing `ExportEngine` attribute used by the image-copy logic.

## Out of scope (from the spec)
Serving a lone PDF un-zipped; custom theming/cover/page-number headers; moving rendering to the worker queue; per-PDF size-limit accuracy.
