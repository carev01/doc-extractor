# Versioned-URL Handling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a product carry a current version, store each source URL as a `{version}` template, and bump the whole product to a new version — re-extracting every source while preserving per-topic change history.

**Architecture:** Version is a Product-level value. Each source stores `url_template` (with a literal `{version}`); `base_url` is that template resolved against `product.version`. Articles gain a version-independent `topic_key` (the URL with the version token swapped for `{version}`); incremental matching keys on `topic_key` instead of `source_url`, so the same topic across versions keeps one `ArticleVersion` timeline. A product-level bump rewrites child URLs and enqueues a normal incremental run per source.

**Tech Stack:** FastAPI, SQLAlchemy (async), Alembic, Pydantic v2, pytest. Spec: `docs/superpowers/specs/2026-06-25-versioned-url-handling-design.md`.

**Scope:** Backend only (data model, version logic, extraction wiring, API). This delivers the full capability and is testable end-to-end via the API. The frontend (product version UI, source `{version}` input, changelog labeling) is a **separate follow-up plan** — it's an independent subsystem with no shared state beyond the API.

## Global Constraints

- New SQLAlchemy models/columns must be reachable from `app/models/__init__.py` (already imported as a package).
- Tests use a **synchronous** psycopg2 `Session` against the `docextractor_test` database (see existing `tests/`); async routes are tested via `httpx.AsyncClient` with `get_db` overridden (see `tests/test_products.py`).
- All new columns are **nullable/additive**; non-versioned sources must behave exactly as today (`topic_key == source_url`).
- `topic_key` is derived from the **persisted `source_url`** (after any profile URL normalization).
- Follow existing route/schema/model patterns; keep the pure version logic DB-free in its own module.

---

### Task 1: Pure version-templating module

**Files:**
- Create: `backend/app/services/versioning.py`
- Test: `backend/tests/test_versioning.py`

**Interfaces:**
- Produces:
  - `resolve_template(template: str, version: str) -> str`
  - `derive_topic_key(url: str, url_template: str | None, version: str | None) -> str`
  - `detect_version_token(base_url: str, version: str) -> str | None`
  - `VERSION_PLACEHOLDER = "{version}"`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_versioning.py
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.versioning import (
    resolve_template, derive_topic_key, detect_version_token, VERSION_PLACEHOLDER,
)

ARC = "https://docs.example.com/UDP/Available/{version}/ENU/SolG/default.htm"


def test_resolve_template_substitutes_version():
    assert resolve_template(ARC, "10.0") == \
        "https://docs.example.com/UDP/Available/10.0/ENU/SolG/default.htm"


def test_derive_topic_key_swaps_version_for_placeholder():
    url = "https://docs.example.com/UDP/Available/10.0/ENU/SolG/install.htm"
    assert derive_topic_key(url, ARC, "10.0") == \
        "https://docs.example.com/UDP/Available/{version}/ENU/SolG/install.htm"


def test_derive_topic_key_is_stable_across_versions():
    u10 = "https://docs.example.com/UDP/Available/10.0/ENU/SolG/install.htm"
    u11 = "https://docs.example.com/UDP/Available/11.0/ENU/SolG/install.htm"
    assert derive_topic_key(u10, ARC, "10.0") == derive_topic_key(u11, ARC, "11.0")


def test_derive_topic_key_only_touches_prefix_occurrence():
    # The version string also appears in the topic slug; only the prefix one is swapped.
    tmpl = "https://docs.example.com/p/{version}/guide.htm"
    url = "https://docs.example.com/p/10.0/whats-new-in-10.0.htm"
    assert derive_topic_key(url, tmpl, "10.0") == \
        "https://docs.example.com/p/{version}/whats-new-in-10.0.htm"


def test_derive_topic_key_passthrough_when_not_templated():
    url = "https://docs.example.com/x/install.htm"
    assert derive_topic_key(url, None, None) == url


def test_detect_version_token_builds_template():
    base = "https://www.dell.com/manuals/pp-dm_20.1_cloud.htm"
    assert detect_version_token(base, "20.1") == \
        "https://www.dell.com/manuals/pp-dm_{version}_cloud.htm"


def test_detect_version_token_none_when_absent():
    assert detect_version_token("https://x/manuals/guide.htm", "20.1") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_versioning.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.versioning'`

- [ ] **Step 3: Write the implementation**

```python
# backend/app/services/versioning.py
"""Version-token templating for sources whose URL embeds the product version.

A source's ``url_template`` holds a literal ``{version}`` placeholder; the live
``base_url`` is the template resolved against the product's current version. A
``topic_key`` is the version-independent identity of an article — its URL with
the version token swapped back to ``{version}`` — so the same topic across
versions shares one key and its history continues across a version bump.
"""

VERSION_PLACEHOLDER = "{version}"


def resolve_template(template: str, version: str) -> str:
    """Substitute the product version into a ``{version}`` URL template."""
    return template.replace(VERSION_PLACEHOLDER, version)


def derive_topic_key(url: str, url_template: str | None, version: str | None) -> str:
    """Return the version-independent key for *url*.

    For a templated source, replace the version token — anchored at the
    template's placeholder offset in the shared URL prefix — with ``{version}``.
    Non-templated sources (or a missing version) return *url* unchanged.
    """
    if not url_template or not version or VERSION_PLACEHOLDER not in url_template:
        return url
    prefix = url_template.split(VERSION_PLACEHOLDER, 1)[0]
    if url.startswith(prefix) and url[len(prefix):len(prefix) + len(version)] == version:
        return prefix + VERSION_PLACEHOLDER + url[len(prefix) + len(version):]
    # Version not at the expected offset — fall back to a single replace so a
    # mildly-divergent URL still keys consistently.
    return url.replace(version, VERSION_PLACEHOLDER, 1)


def detect_version_token(base_url: str, version: str) -> str | None:
    """Return a ``url_template`` (the first occurrence of *version* in *base_url*
    replaced by ``{version}``), or None when the version string isn't present."""
    if not version or version not in base_url:
        return None
    return base_url.replace(version, VERSION_PLACEHOLDER, 1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/test_versioning.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/versioning.py backend/tests/test_versioning.py
git commit -m "feat(versioning): pure {version} template + topic_key helpers"
```

---

### Task 2: Schema migration + model columns

**Files:**
- Create: `backend/alembic/versions/<generated>_add_versioned_url_handling.py`
- Modify: `backend/app/models/product.py`, `backend/app/models/source.py`, `backend/app/models/article.py`, `backend/app/models/extraction_run.py`
- Test: `backend/tests/test_versioning_model.py`

**Interfaces:**
- Produces columns: `Product.version`, `Product.previous_version` (str|None); `DocumentationSource.url_template` (str|None); `ExtractionRun.version` (str|None); `Article.topic_key` (str, NOT NULL, unique with `source_id`).

- [ ] **Step 1: Add the model columns**

In `backend/app/models/product.py`, add after the existing scalar columns:

```python
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    previous_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
```

In `backend/app/models/source.py`, add after `base_url`:

```python
    # Base URL with a literal "{version}" placeholder. NULL = non-versioned source.
    url_template: Mapped[str | None] = mapped_column(String(2048), nullable=True)
```

In `backend/app/models/extraction_run.py`, add a scalar column:

```python
    # Product version captured at run time (NULL for non-versioned products).
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
```

In `backend/app/models/article.py`, add after `source_url`:

```python
    # Version-independent topic identity; unique per source. Equals source_url
    # for non-versioned sources.
    topic_key: Mapped[str] = mapped_column(String(2048), nullable=False)
```

Add a table-level unique constraint to `Article` (inside the class, e.g. below the columns):

```python
    __table_args__ = (
        UniqueConstraint("source_id", "topic_key", name="uq_articles_source_topic"),
    )
```

Ensure `UniqueConstraint` is imported in `article.py`:

```python
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
```

- [ ] **Step 2: Generate the migration skeleton**

Run: `cd backend && alembic revision -m "add versioned url handling"`
Expected: creates a new file under `alembic/versions/` with `down_revision` pre-set to the current head.

- [ ] **Step 3: Fill in upgrade/downgrade**

Replace the generated `upgrade()`/`downgrade()` bodies with:

```python
import sqlalchemy as sa
from alembic import op


def upgrade() -> None:
    op.add_column("products", sa.Column("version", sa.String(64), nullable=True))
    op.add_column("products", sa.Column("previous_version", sa.String(64), nullable=True))
    op.add_column("documentation_sources", sa.Column("url_template", sa.String(2048), nullable=True))
    op.add_column("extraction_runs", sa.Column("version", sa.String(64), nullable=True))
    op.add_column("articles", sa.Column("topic_key", sa.String(2048), nullable=True))
    # Backfill existing rows so the matching key is unchanged for non-versioned sources.
    op.execute("UPDATE articles SET topic_key = source_url WHERE topic_key IS NULL")
    op.alter_column("articles", "topic_key", nullable=False)
    op.create_unique_constraint(
        "uq_articles_source_topic", "articles", ["source_id", "topic_key"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_articles_source_topic", "articles", type_="unique")
    op.drop_column("articles", "topic_key")
    op.drop_column("extraction_runs", "version")
    op.drop_column("documentation_sources", "url_template")
    op.drop_column("products", "previous_version")
    op.drop_column("products", "version")
```

- [ ] **Step 4: Write the model test**

```python
# backend/tests/test_versioning_model.py
import os, sys, uuid
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.models import Product, DocumentationSource, Article, ExtractionRun


def test_new_columns_exist_on_models():
    assert hasattr(Product, "version") and hasattr(Product, "previous_version")
    assert hasattr(DocumentationSource, "url_template")
    assert hasattr(ExtractionRun, "version")
    assert hasattr(Article, "topic_key")
```

- [ ] **Step 5: Apply migration and run the test**

Run:
```bash
cd backend && alembic upgrade head && python3 -m pytest tests/test_versioning_model.py -q
```
Expected: migration applies cleanly; test PASS (1 passed).

- [ ] **Step 6: Commit**

```bash
git add backend/app/models backend/alembic/versions backend/tests/test_versioning_model.py
git commit -m "feat(db): add version columns + article topic_key (migration)"
```

---

### Task 3: Match on topic_key + tag runs with version (extraction)

**Files:**
- Modify: `backend/app/services/firecrawl.py` (`process_article_result`, `extract_source`, `_poll_batch_and_process`, `_scrape_via_browserless`)
- Test: `backend/tests/test_versioning_match.py`

**Interfaces:**
- Consumes: `derive_topic_key` (Task 1); `Article.topic_key`, `ExtractionRun.version` (Task 2).
- Produces: `process_article_result(..., topic_key: str | None = None, ...)` — matches existing articles by `(source_id, topic_key)`, sets `article.topic_key`, and updates `source_url` on match.

> **Why `reconcile_removals` is unchanged:** when a topic survives a bump, `process_article_result` updates its `source_url` to the new-version URL, so after the content phase every surviving article's `source_url` again equals its (new) TOC entry URL. The existing `reconcile_removals` (matching `TOCEntry.url == Article.source_url`) therefore still re-links survivors and flags only truly-removed topics. Do **not** change it.

- [ ] **Step 1: Add `topic_key` param + matching to `process_article_result`**

In `process_article_result`, add the parameter (after `url`):

```python
        url: str,
        topic_key: str | None = None,
```

At the top of the method body (after the docstring), add:

```python
        match_key = topic_key or url
```

Replace the `change_status == "same"` update's WHERE clause:

```python
                .where(Article.source_id == source_id, Article.source_url == url)
```
with:
```python
                .where(Article.source_id == source_id, Article.topic_key == match_key)
```

Replace the `existing_result` select WHERE clause:

```python
            select(Article).where(
                Article.source_id == source_id,
                Article.source_url == url,
            )
```
with:
```python
            select(Article).where(
                Article.source_id == source_id,
                Article.topic_key == match_key,
            )
```

In the `existing_article is not None` update block, after `article.source_url = url`, add:

```python
            article.topic_key = match_key
```

In the `else:` new-`Article(...)` construction, add `topic_key=match_key` to the kwargs (alongside `source_url=url`).

- [ ] **Step 2: Thread topic_key + run.version through `extract_source`**

Add the import near the top of `firecrawl.py`:

```python
from app.services.versioning import derive_topic_key
```

In `extract_source`, after the source is loaded and before building `toc_entries`, load the product version and tag the run:

```python
            product_version = (
                await db.execute(
                    select(Product.version).where(Product.id == source.product_id)
                )
            ).scalar_one_or_none()
            run.version = product_version
```
(Ensure `Product` is imported in `firecrawl.py`: `from app.models.product import Product`.)

In the `toc_entries = [ { ... } for e in toc_objs ]` comprehension, add a `topic_key` field:

```python
                    "topic_key": derive_topic_key(e.url, source.url_template, product_version),
```

- [ ] **Step 3: Pass topic_key at the two call sites**

In `_scrape_via_browserless`, the `process_article_result(...)` call: add
```python
                            topic_key=entry.get("topic_key"),
```

In `_poll_batch_and_process`, find each `process_article_result(...)` call and add the same `topic_key=entry.get("topic_key"),` argument (the per-URL `entry` dict is already in scope there).

- [ ] **Step 4: Write the matching test**

```python
# backend/tests/test_versioning_match.py
# Sync-DB test mirroring tests/test_versions.py harness: build a source with an
# article at v10.0, then re-run process_article_result with the v11.0 URL but the
# SAME topic_key and assert the same article row is updated (history preserved).
import os, sys, uuid, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest
from sqlalchemy import select
from app.models import Article, ArticleVersion
from app.services.versioning import derive_topic_key

# Reuse the async-session + FirecrawlService fixtures from the existing suite.
from tests.helpers_versioning import make_service_and_source  # see Step 5

TMPL = "https://docs.example.com/UDP/Available/{version}/ENU/SolG/install.htm"

pytestmark = pytest.mark.asyncio


async def test_bump_matches_by_topic_key_and_appends_version(db_session):
    svc, source = await make_service_and_source(db_session, url_template=TMPL, version="10.0")
    run = await _make_run(db_session, source)  # helper: PENDING run for source
    key = derive_topic_key(TMPL.replace("{version}", "10.0"), TMPL, "10.0")
    await svc.process_article_result(
        db=db_session, source_id=source.id, run_id=run.id,
        url=TMPL.replace("{version}", "10.0"), topic_key=key,
        markdown_content="v10 body", doc_html="", toc_entry_id=None,
        sort_order=0, title="Install",
    )
    art = (await db_session.execute(select(Article).where(Article.source_id == source.id))).scalar_one()
    assert art.topic_key == key and "10.0" in art.source_url

    # Same topic, new version URL — must update the SAME row + add a version.
    run2 = await _make_run(db_session, source)
    await svc.process_article_result(
        db=db_session, source_id=source.id, run_id=run2.id,
        url=TMPL.replace("{version}", "11.0"), topic_key=key,
        markdown_content="v11 body", doc_html="", toc_entry_id=None,
        sort_order=0, title="Install",
    )
    arts = (await db_session.execute(select(Article).where(Article.source_id == source.id))).scalars().all()
    assert len(arts) == 1                    # same row, not a new article
    assert "11.0" in arts[0].source_url       # source_url advanced
    versions = (await db_session.execute(
        select(ArticleVersion).where(ArticleVersion.article_id == arts[0].id)
    )).scalars().all()
    assert len(versions) == 1                 # the v10 snapshot was archived
```

- [ ] **Step 5: Add the small test helper**

Create `backend/tests/helpers_versioning.py` with `make_service_and_source(db_session, url_template, version)` and `_make_run(db_session, source)` that insert a Vendor→Product(version=version)→Source(url_template, base_url resolved) and a PENDING `ExtractionRun`, returning the `FirecrawlService` and source. Model it on the setup already used in `tests/test_versions.py` (read that file and copy its engine/session + factory pattern; do not invent a new harness).

- [ ] **Step 6: Run the test**

Run: `cd backend && python3 -m pytest tests/test_versioning_match.py -q`
Expected: PASS (1 passed). Also run `python3 -m pytest tests/test_versions.py -q` to confirm existing incremental/version behavior still passes.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/firecrawl.py backend/tests/test_versioning_match.py backend/tests/helpers_versioning.py
git commit -m "feat(extraction): match articles by topic_key; tag runs with version"
```

---

### Task 4: Source `url_template` schema + version-token detection route

**Files:**
- Modify: `backend/app/schemas/source.py` (add `url_template`)
- Modify: `backend/app/routes/sources.py` (accept `url_template`; resolve `base_url`; add detect route)
- Test: `backend/tests/test_versioning_routes.py`

**Interfaces:**
- Consumes: `resolve_template`, `detect_version_token` (Task 1).
- Produces: `POST /api/sources/{id}/detect-version-token` `{ "version": str }` → `{ "url_template": str | None }`. Source create/update accept optional `url_template`.

- [ ] **Step 1: Add `url_template` to the source schemas**

In `backend/app/schemas/source.py`, add `url_template: str | None = None` to `SourceCreate`, `SourceUpdate`, and `SourceResponse` (match the existing field style/imports).

- [ ] **Step 2: Write the failing route test**

```python
# backend/tests/test_versioning_routes.py  (uses the httpx.AsyncClient harness
# from tests/test_products.py — copy its `client`/`get_db` override fixtures)
import pytest
pytestmark = pytest.mark.asyncio


async def test_detect_version_token_proposes_template(client, seeded_source):
    # seeded_source.base_url == "https://x/UDP/Available/10.0/SolG/default.htm"
    r = await client.post(
        f"/api/sources/{seeded_source.id}/detect-version-token",
        json={"version": "10.0"},
    )
    assert r.status_code == 200
    assert r.json()["url_template"] == "https://x/UDP/Available/{version}/SolG/default.htm"


async def test_detect_version_token_none_when_absent(client, seeded_source):
    r = await client.post(
        f"/api/sources/{seeded_source.id}/detect-version-token",
        json={"version": "99.9"},
    )
    assert r.status_code == 200 and r.json()["url_template"] is None
```

- [ ] **Step 3: Run it to verify it fails**

Run: `cd backend && python3 -m pytest tests/test_versioning_routes.py -q`
Expected: FAIL — 404 (route not defined).

- [ ] **Step 4: Implement the detect route + url_template handling**

In `backend/app/routes/sources.py` add (import `detect_version_token`, `resolve_template` from `app.services.versioning`, and `BaseModel` if needed):

```python
class _DetectTokenBody(BaseModel):
    version: str


@router.post("/{source_id}/detect-version-token")
async def detect_version_token_route(
    source_id: uuid.UUID, body: _DetectTokenBody, db: AsyncSession = Depends(get_db)
):
    source = await db.get(DocumentationSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return {"url_template": detect_version_token(source.base_url, body.version)}
```

In the source **create** and **update** handlers, when `url_template` is provided, also keep `base_url` consistent: if the product has a version, set `base_url = resolve_template(url_template, product.version)`. (Read the product via `source.product_id`.) Persist `url_template` as given.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/test_versioning_routes.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/schemas/source.py backend/app/routes/sources.py backend/tests/test_versioning_routes.py
git commit -m "feat(api): source url_template + detect-version-token route"
```

---

### Task 5: Product "enable versioning" (templatize + key backfill) route

**Files:**
- Modify: `backend/app/routes/products.py`
- Test: `backend/tests/test_versioning_enable.py`

**Interfaces:**
- Consumes: `detect_version_token`, `derive_topic_key` (Task 1).
- Produces: `POST /api/products/{id}/versions/enable` `{ "version": str }` → templatizes every child source whose `base_url` contains the version, sets `product.version`, and **recomputes `topic_key` for those sources' existing articles** so the first bump doesn't break history.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_versioning_enable.py
import pytest
from sqlalchemy import select
from app.models import Article, DocumentationSource
pytestmark = pytest.mark.asyncio


async def test_enable_templatizes_and_rekeys(client, db_session, seeded_product_10):
    # seeded_product_10: product + 1 source base_url ".../Available/10.0/.../a.htm"
    # with one article whose topic_key currently equals its full 10.0 source_url.
    r = await client.post(
        f"/api/products/{seeded_product_10.id}/versions/enable",
        json={"version": "10.0"},
    )
    assert r.status_code == 200
    src = (await db_session.execute(select(DocumentationSource)
        .where(DocumentationSource.product_id == seeded_product_10.id))).scalar_one()
    assert "{version}" in src.url_template
    art = (await db_session.execute(select(Article)
        .where(Article.source_id == src.id))).scalar_one()
    assert "{version}" in art.topic_key      # rekeyed, ready for a future bump
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && python3 -m pytest tests/test_versioning_enable.py -q`
Expected: FAIL — 404.

- [ ] **Step 3: Implement the enable route**

In `backend/app/routes/products.py` (import `select`, `update`, the models, and `detect_version_token`, `derive_topic_key`):

```python
class _EnableVersionBody(BaseModel):
    version: str


@router.post("/{product_id}/versions/enable")
async def enable_versioning(
    product_id: uuid.UUID, body: _EnableVersionBody, db: AsyncSession = Depends(get_db)
):
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    sources = (await db.execute(
        select(DocumentationSource).where(DocumentationSource.product_id == product_id)
    )).scalars().all()
    templatized = 0
    for src in sources:
        tmpl = detect_version_token(src.base_url, body.version)
        if tmpl is None:
            continue
        src.url_template = tmpl
        templatized += 1
        # Rekey existing articles so a later bump matches by version-independent key.
        arts = (await db.execute(
            select(Article).where(Article.source_id == src.id)
        )).scalars().all()
        for art in arts:
            art.topic_key = derive_topic_key(art.source_url, tmpl, body.version)
    product.version = body.version
    await db.commit()
    return {"version": product.version, "templatized_sources": templatized}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd backend && python3 -m pytest tests/test_versioning_enable.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routes/products.py backend/tests/test_versioning_enable.py
git commit -m "feat(api): product enable-versioning (templatize + rekey)"
```

---

### Task 6: Product version-bump route

**Files:**
- Modify: `backend/app/routes/products.py`
- Test: `backend/tests/test_versioning_bump.py`

**Interfaces:**
- Consumes: `resolve_template` (Task 1); `enqueue_run` from `app.services.queue`.
- Produces: `POST /api/products/{id}/versions/bump` `{ "version": str }` → rewrites templated child `base_url`s, sets `product.version`/`previous_version`, enqueues one incremental run per affected source; returns `{ "version", "runs": [run_id, ...] }`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_versioning_bump.py
import pytest
from sqlalchemy import select
from app.models import DocumentationSource, ExtractionRun, Product
pytestmark = pytest.mark.asyncio


async def test_bump_rewrites_urls_and_enqueues_runs(client, db_session, seeded_templated_product):
    # product.version == "10.0"; one templated source ".../Available/{version}/.../a.htm".
    r = await client.post(
        f"/api/products/{seeded_templated_product.id}/versions/bump",
        json={"version": "11.0"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "11.0" and len(body["runs"]) == 1

    prod = await db_session.get(Product, seeded_templated_product.id)
    assert prod.version == "11.0" and prod.previous_version == "10.0"
    src = (await db_session.execute(select(DocumentationSource)
        .where(DocumentationSource.product_id == prod.id))).scalar_one()
    assert "/11.0/" in src.base_url
    run = (await db_session.execute(select(ExtractionRun)
        .where(ExtractionRun.source_id == src.id))).scalar_one()
    assert run.status.value == "pending"


async def test_bump_rejects_when_no_templated_sources(client, seeded_product_plain):
    r = await client.post(
        f"/api/products/{seeded_product_plain.id}/versions/bump",
        json={"version": "2.0"},
    )
    assert r.status_code == 400
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && python3 -m pytest tests/test_versioning_bump.py -q`
Expected: FAIL — 404.

- [ ] **Step 3: Implement the bump route**

In `backend/app/routes/products.py` (import `resolve_template`, `enqueue_run`, `ActiveRunExists`):

```python
class _BumpVersionBody(BaseModel):
    version: str


@router.post("/{product_id}/versions/bump")
async def bump_version(
    product_id: uuid.UUID, body: _BumpVersionBody, db: AsyncSession = Depends(get_db)
):
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    if not body.version or body.version == product.version:
        raise HTTPException(status_code=400, detail="Provide a new, different version")
    sources = (await db.execute(
        select(DocumentationSource).where(
            DocumentationSource.product_id == product_id,
            DocumentationSource.url_template.isnot(None),
        )
    )).scalars().all()
    templated = [s for s in sources if "{version}" in (s.url_template or "")]
    if not templated:
        raise HTTPException(
            status_code=400, detail="No templated ({version}) sources to bump"
        )
    product.previous_version = product.version
    product.version = body.version
    for s in templated:
        s.base_url = resolve_template(s.url_template, body.version)
    await db.commit()

    run_ids = []
    for s in templated:
        try:
            run = await enqueue_run(db, s.id, trigger="version-bump")
            run_ids.append(str(run.id))
        except ActiveRunExists:
            continue  # a run is already queued/active for this source; skip
    return {"version": product.version, "runs": run_ids}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/test_versioning_bump.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Full regression**

Run: `cd backend && python3 -m pytest -q`
Expected: all pass (DB required). Fix any breakage before committing.

- [ ] **Step 6: Commit**

```bash
git add backend/app/routes/products.py backend/tests/test_versioning_bump.py
git commit -m "feat(api): product version-bump rewrites URLs + enqueues runs"
```

---

## Self-review notes

- **Spec coverage:** data model → Task 2; topic_key + matching → Tasks 1, 3; bump flow → Task 6; enable/templatize bridge → Task 5; detect-version-token → Task 4; `run.version` tagging → Task 3. `reconcile_removals` deliberately unchanged (rationale in Task 3) — a refinement of the spec, documented there. Frontend (Section "Frontend" of the spec) is intentionally deferred to a separate plan.
- **Type consistency:** `derive_topic_key(url, url_template, version)`, `resolve_template(template, version)`, `detect_version_token(base_url, version)`, and `process_article_result(..., topic_key=...)` are used identically wherever referenced.
- **Out of scope (unchanged):** vendor probing for new versions; browsable per-version snapshots.

## Follow-up (separate plan)

Frontend: product version display + "Bump version" / "Enable versioning" actions, source `{version}` input with live preview, and `10.0 → 11.0` boundary labeling in the changelog. No backend changes required — consumes the routes above.
