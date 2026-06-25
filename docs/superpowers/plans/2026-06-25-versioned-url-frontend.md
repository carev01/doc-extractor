# Versioned-URL Handling — Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the versioned-URL backend (PR #60) in the UI — enable/bump a product version from the Sources view, auto-detect `{version}` templates on the source URL input, and label changelog/history snapshots with their product version.

**Architecture:** Two small backend response additions expose existing version columns; the frontend adds a version bar + bump modal in the Sources view, an auto-detect toggle on the source URL input, and version tags + boundary dividers in the history/changelog. State stays in `App.tsx`/`SourceList` as today; new UI is leaf components calling the API client.

**Tech Stack:** Backend: FastAPI, SQLAlchemy (async), Pydantic v2, pytest. Frontend: React 19 + TypeScript + Vite. Spec: `docs/superpowers/specs/2026-06-25-versioned-url-frontend-design.md`.

## Global Constraints

- **Backend tests** use the sync psycopg2 / httpx.AsyncClient harness against `docextractor_test` (see `tests/test_products.py`, `tests/test_versions.py`). A local Postgres 16 with `docextractor`/`docextractor_dev` @ `localhost:5432` and the `docextractor_test` DB is available; interpreter is `python3`.
- **The frontend has no unit-test framework.** Each frontend task's gate is: `cd frontend && npm run build` (type-check, must succeed) **and** `npm run lint` (must introduce **no new** errors beyond the repo's pre-existing baseline — capture the baseline count first). There is no `npm test`.
- `{version}` is the literal placeholder string. A source's live `base_url` = `url_template` with `{version}` replaced by the product version.
- Non-versioned products/sources must keep working exactly as today (all new fields are optional/nullable).
- Follow existing patterns: route/schema style in `backend/app/`, and component/state style in `frontend/src/` (e.g. `SourceList.tsx`, `api/client.ts`, `types/index.ts`).

---

### Task 1: Expose product version on `ProductResponse`

**Files:**
- Modify: `backend/app/schemas/product.py`
- Test: `backend/tests/test_product_version_response.py`

**Interfaces:**
- Produces: `ProductResponse.version: str | None`, `ProductResponse.previous_version: str | None` (populated from the `Product` ORM columns via `from_attributes`).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_product_version_response.py  (httpx harness from tests/test_products.py)
import pytest
pytestmark = pytest.mark.asyncio


async def test_product_response_includes_version_fields(client, db_session):
    # Seed a vendor + product with version set, then GET it and assert the fields.
    from app.models import Vendor, Product
    async with db_session() as s:
        v = Vendor(name="Vv"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="Pp", version="10.0", previous_version="9.0")
        s.add(p); await s.commit(); pid = p.id
    r = await client.get(f"/api/products/{pid}")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "10.0"
    assert body["previous_version"] == "9.0"
```

(Use the same `client`/`db_session` fixtures as `tests/test_products.py`; copy them into this file.)

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && python3 -m pytest tests/test_product_version_response.py -q`
Expected: FAIL — `KeyError: 'version'` (field not serialized).

- [ ] **Step 3: Add the fields**

In `backend/app/schemas/product.py`, inside `class ProductResponse`, after `name: str`:

```python
    version: str | None = None
    previous_version: str | None = None
```

- [ ] **Step 4: Run it to verify it passes**

Run: `cd backend && python3 -m pytest tests/test_product_version_response.py tests/test_products.py -q`
Expected: PASS (new test + existing product tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/schemas/product.py backend/tests/test_product_version_response.py
git commit -m "feat(api): expose product version/previous_version on ProductResponse"
```

---

### Task 2: Expose run version on version-history + changelog responses

**Files:**
- Modify: `backend/app/schemas/version.py` (add `version` to `ArticleVersionResponse` and `ChangelogEntry`)
- Modify: `backend/app/routes/articles.py` (`list_article_versions` — join `ExtractionRun.version`)
- Modify: `backend/app/routes/sources.py` (`get_source_changelog` — join `ExtractionRun.version`)
- Test: `backend/tests/test_version_labeling_response.py`

**Interfaces:**
- Produces: `ArticleVersionResponse.version: str | None`, `ChangelogEntry.version: str | None` (the `ExtractionRun.version` of the snapshot's/entry's `extraction_run_id`).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_version_labeling_response.py  (httpx harness)
import pytest
pytestmark = pytest.mark.asyncio


async def test_article_versions_carry_run_version(client, db_session):
    from app.models import Vendor, Product, DocumentationSource, Article, ArticleVersion
    from app.models.extraction_run import ExtractionRun, RunStatus
    async with db_session() as s:
        v = Vendor(name="V2"); s.add(v); await s.flush()
        p = Product(vendor_id=v.id, name="P2", version="11.0"); s.add(p); await s.flush()
        src = DocumentationSource(product_id=p.id, name="S2",
                                  base_url="https://x/11.0/a", topic_key="https://x/{version}/a")
        s.add(src); await s.flush()
        run = ExtractionRun(source_id=src.id, status=RunStatus.COMPLETED, version="11.0")
        s.add(run); await s.flush()
        art = Article(source_id=src.id, title="A", source_url="https://x/11.0/a",
                      topic_key="https://x/{version}/a", content_markdown="now")
        s.add(art); await s.flush()
        ver = ArticleVersion(article_id=art.id, extraction_run_id=run.id,
                             content_markdown="old", content_hash="h")
        s.add(ver); await s.commit(); aid = art.id
    r = await client.get(f"/api/articles/{aid}/versions")
    assert r.status_code == 200
    assert r.json()["versions"][0]["version"] == "11.0"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && python3 -m pytest tests/test_version_labeling_response.py -q`
Expected: FAIL — `KeyError: 'version'`.

- [ ] **Step 3: Add the schema fields**

In `backend/app/schemas/version.py`, add to `class ArticleVersionResponse` (after `extracted_at`):

```python
    version: str | None = None  # product version of the run that superseded this snapshot
```

and to `class ChangelogEntry` (after `extraction_run_id`):

```python
    version: str | None = None  # product version of the entry's extraction run
```

- [ ] **Step 4: Join the run version in `list_article_versions`**

In `backend/app/routes/articles.py`, in `list_article_versions`: import `ExtractionRun` (`from app.models.extraction_run import ExtractionRun`), add `ExtractionRun.version` to the select and a LEFT OUTER JOIN, then pass it through:

```python
        select(
            ArticleVersion.id,
            ArticleVersion.article_id,
            ArticleVersion.extraction_run_id,
            ArticleVersion.content_hash,
            ArticleVersion.diff_text.isnot(None).label("has_diff"),
            func.coalesce(
                func.octet_length(ArticleVersion.content_markdown), 0
            ).label("content_size_bytes"),
            ArticleVersion.extracted_at,
            ExtractionRun.version.label("run_version"),
        )
        .outerjoin(ExtractionRun, ExtractionRun.id == ArticleVersion.extraction_run_id)
        .where(ArticleVersion.article_id == article_id)
        .order_by(ArticleVersion.extracted_at.desc())
        .offset(skip)
        .limit(limit)
```

and in the `ArticleVersionResponse(...)` construction add `version=r.run_version,`.

- [ ] **Step 5: Join the run version in `get_source_changelog`**

In `backend/app/routes/sources.py`, in `get_source_changelog`: after the `events = union_all(*parts).subquery()`, change the final `select(events)` to LEFT JOIN `ExtractionRun` on the events' `extraction_run_id` and select its version. Concretely, replace the paginated `select(events)…` with:

```python
    rows_q = (
        select(events, ExtractionRun.version.label("run_version"))
        .select_from(events)
        .outerjoin(ExtractionRun, ExtractionRun.id == events.c.extraction_run_id)
        .order_by(events.c.timestamp.desc())
        .offset(skip)
        .limit(limit)
    )
    rows = (await db.execute(rows_q)).all()
```

(Keep the existing `order_by`/`offset`/`limit` values — copy them from the current query.) Then in the `ChangelogEntry(...)` construction add `version=r.run_version,`. `ExtractionRun` is already imported in this file.

- [ ] **Step 6: Run the tests**

Run: `cd backend && python3 -m pytest tests/test_version_labeling_response.py tests/test_versions.py -q`
Expected: PASS (new test + the existing changelog/version suite).

- [ ] **Step 7: Commit**

```bash
git add backend/app/schemas/version.py backend/app/routes/articles.py backend/app/routes/sources.py backend/tests/test_version_labeling_response.py
git commit -m "feat(api): surface run version on article-version + changelog responses"
```

---

### Task 3: Frontend types + API client

**Files:**
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/api/client.ts`

**Interfaces:**
- Produces (types): `Product.version: string | null`, `Product.previous_version: string | null`; `ArticleVersion.version: string | null`; `ChangelogEntry.version: string | null`; `DocumentationSource.url_template: string | null` (if not already present).
- Produces (client): `bumpProductVersion(productId, version) => Promise<{ version: string; runs: string[] }>`; `enableProductVersioning(productId, version) => Promise<{ version: string; templatized_sources: number }>`; `detectVersionToken(sourceId, version) => Promise<{ url_template: string | null }>`. Extend `createSource`/`updateSource` payloads with optional `url_template`.

- [ ] **Step 1: Capture the lint baseline**

Run: `cd frontend && npm run lint 2>&1 | tail -1`
Record the problem count (e.g. "29 problems") — later steps must not exceed it.

- [ ] **Step 2: Add the type fields**

In `frontend/src/types/index.ts`:
- In `interface Product`, add `version: string | null;` and `previous_version: string | null;`.
- In `interface ArticleVersion`, add `version: string | null;`.
- In `interface ChangelogEntry`, add `version: string | null;`.
- In `interface DocumentationSource`, add `url_template: string | null;` if not already present.

- [ ] **Step 3: Add the API client functions**

In `frontend/src/api/client.ts`, add (near the products section):

```typescript
export async function bumpProductVersion(
  productId: string,
  version: string,
): Promise<{ version: string; runs: string[] }> {
  const res = await api.post(`/products/${productId}/versions/bump`, { version });
  return res.data;
}

export async function enableProductVersioning(
  productId: string,
  version: string,
): Promise<{ version: string; templatized_sources: number }> {
  const res = await api.post(`/products/${productId}/versions/enable`, { version });
  return res.data;
}

export async function detectVersionToken(
  sourceId: string,
  version: string,
): Promise<{ url_template: string | null }> {
  const res = await api.post(`/sources/${sourceId}/detect-version-token`, { version });
  return res.data;
}
```

Extend the `createSource` payload type to include `url_template?: string` and `platform?: string`, and the `updateSource` payload type to include `url_template?: string | null`. (Read the current signatures at `client.ts:111` and `:136` and add the optional fields; the body is forwarded as-is.)

- [ ] **Step 4: Build + lint**

Run: `cd frontend && npm run build && npm run lint 2>&1 | tail -1`
Expected: build succeeds; lint problem count ≤ the Step 1 baseline.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/api/client.ts
git commit -m "feat(ui): version types + bump/enable/detect API client functions"
```

---

### Task 4: ProductVersionBar + BumpVersionModal in the Sources view

**Files:**
- Create: `frontend/src/components/ProductVersionBar.tsx`
- Create: `frontend/src/components/BumpVersionModal.tsx`
- Modify: `frontend/src/components/SourceList.tsx` (render `ProductVersionBar` above the source list; refresh on change)
- Modify: `frontend/src/App.tsx` (refresh `selectedProduct` after enable/bump so the bar reflects the new version)

**Interfaces:**
- Consumes: `Product`, `DocumentationSource` types; `enableProductVersioning`, `bumpProductVersion` (Task 3).
- Produces: `<ProductVersionBar product={Product} sources={DocumentationSource[]} onChanged={() => void} />`; `<BumpVersionModal product sources onClose onBumped />`.

- [ ] **Step 1: Create `ProductVersionBar.tsx`**

```tsx
import { useState } from "react";
import type { Product, DocumentationSource } from "../types";
import { enableProductVersioning } from "../api/client";
import BumpVersionModal from "./BumpVersionModal";

interface Props {
  product: Product;
  sources: DocumentationSource[];
  onChanged: () => void;
}

export default function ProductVersionBar({ product, sources, onChanged }: Props) {
  const [enabling, setEnabling] = useState(false);
  const [enableValue, setEnableValue] = useState("");
  const [showBump, setShowBump] = useState(false);
  const [error, setError] = useState("");

  const submitEnable = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!enableValue.trim()) return;
    setError("");
    try {
      await enableProductVersioning(product.id, enableValue.trim());
      setEnabling(false);
      setEnableValue("");
      onChanged();
    } catch {
      setError("Failed to enable versioning");
    }
  };

  return (
    <div className="version-bar">
      {product.version ? (
        <>
          <span className="version-badge">Version: {product.version}</span>
          <button type="button" className="btn-secondary-sm" onClick={() => setShowBump(true)}>
            Bump version
          </button>
        </>
      ) : enabling ? (
        <form onSubmit={submitEnable} className="version-enable-form">
          <input
            autoFocus
            placeholder="Current version (e.g. 10.0)"
            value={enableValue}
            onChange={(e) => setEnableValue(e.target.value)}
          />
          <button type="submit">Enable</button>
          <button type="button" className="btn-secondary-sm" onClick={() => setEnabling(false)}>
            Cancel
          </button>
        </form>
      ) : (
        <button type="button" className="btn-secondary-sm" onClick={() => setEnabling(true)}>
          Enable versioning
        </button>
      )}
      {error && <span className="error-inline">{error}</span>}
      {showBump && (
        <BumpVersionModal
          product={product}
          sources={sources}
          onClose={() => setShowBump(false)}
          onBumped={() => {
            setShowBump(false);
            onChanged();
          }}
        />
      )}
    </div>
  );
}
```

- [ ] **Step 2: Create `BumpVersionModal.tsx`**

```tsx
import { useState } from "react";
import type { Product, DocumentationSource } from "../types";
import { bumpProductVersion } from "../api/client";

interface Props {
  product: Product;
  sources: DocumentationSource[];
  onClose: () => void;
  onBumped: () => void;
}

export default function BumpVersionModal({ product, sources, onClose, onBumped }: Props) {
  const [newVersion, setNewVersion] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const templated = sources.filter((s) => s.url_template && s.url_template.includes("{version}"));
  const v = newVersion.trim();
  const canSubmit = v !== "" && v !== product.version && templated.length > 0;

  const preview = (s: DocumentationSource) =>
    (s.url_template as string).replaceAll("{version}", v || "{version}");

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setError("");
    try {
      await bumpProductVersion(product.id, v);
      onBumped();
    } catch {
      setError("Bump failed");
      setBusy(false);
    }
  };

  return (
    <div className="overlay-backdrop" onClick={onClose}>
      <div className="overlay-panel" onClick={(e) => e.stopPropagation()}>
        <h3>Bump {product.name} from {product.version} → {v || "?"}</h3>
        <input
          autoFocus
          placeholder="New version (e.g. 11.0)"
          value={newVersion}
          onChange={(e) => setNewVersion(e.target.value)}
        />
        <p className="muted">{templated.length} templated source(s) will be rewritten and re-extracted:</p>
        <ul className="bump-preview">
          {templated.map((s) => (
            <li key={s.id}>
              <code>{s.base_url}</code> → <code>{preview(s)}</code>
            </li>
          ))}
        </ul>
        {sources.length > templated.length && (
          <p className="muted">{sources.length - templated.length} non-templated source(s) unaffected.</p>
        )}
        {error && <span className="error-inline">{error}</span>}
        <div className="overlay-actions">
          <button type="button" disabled={!canSubmit || busy} onClick={submit}>
            {busy ? "Bumping…" : "Bump & re-extract"}
          </button>
          <button type="button" className="btn-secondary-sm" onClick={onClose}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Mount the bar in `SourceList.tsx`**

Read `SourceList.tsx`. It already holds `sources` state and the `product` prop. Import `ProductVersionBar`, and render it at the top of the component's returned JSX (just inside the outer wrapper, before the add-source `<form>`):

```tsx
      <ProductVersionBar product={product} sources={sources} onChanged={fetchSources} />
```

`fetchSources` already exists (it reloads the source list). For the version *badge* to update after enable/bump, the `product` prop must refresh too — handled in Step 4.

- [ ] **Step 4: Refresh the product after enable/bump in `App.tsx`**

`SourceList` receives `product={selectedProduct}` from `App.tsx`. Add an `onProductChanged` prop to `SourceList` that `ProductVersionBar`'s `onChanged` also calls, wired in `App.tsx` to re-fetch the selected product (use the existing products API — `getProduct(id)` if present, else re-fetch the vendor's product list and pick `selectedProduct.id`) and `setSelectedProduct(updated)`. Read `App.tsx` and `ProductList.tsx` to reuse the existing product-fetch call; do not invent a new endpoint.

Minimal wiring: in `SourceList` props add `onProductChanged?: () => void`; have `ProductVersionBar`'s `onChanged` call both `fetchSources()` and `onProductChanged?.()`.

- [ ] **Step 5: Build + lint + manual check**

Run: `cd frontend && npm run build && npm run lint 2>&1 | tail -1`
Expected: build succeeds; lint ≤ baseline.
Manual: with the dev server (`npm run dev`, backend running), open a product's Sources view → the bar shows "Enable versioning"; after enabling, it shows the version + "Bump version"; the bump modal previews `old → new` URLs.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/ProductVersionBar.tsx frontend/src/components/BumpVersionModal.tsx frontend/src/components/SourceList.tsx frontend/src/App.tsx
git commit -m "feat(ui): product version bar + bump modal in the Sources view"
```

---

### Task 5: Auto-detect `{version}` templating on the source URL input

**Files:**
- Modify: `frontend/src/components/SourceList.tsx` (add-source form + per-source edit)

**Interfaces:**
- Consumes: `Product.version`, `createSource({ url_template })`, `updateSource(id, { url_template })`, `detectVersionToken` (Task 3).

- [ ] **Step 1: Add-source form auto-detect**

Read the `handleCreate` + add-source `<form>` in `SourceList.tsx`. Add state `const [templatize, setTemplatize] = useState(true);`. Below the base-URL `<input>`, when `product.version` is set and the typed `baseUrl` contains `product.version`, render:

```tsx
      {product.version && baseUrl.includes(product.version) && (
        <label className="templatize-hint">
          <input
            type="checkbox"
            checked={templatize}
            onChange={(e) => setTemplatize(e.target.checked)}
          />
          Detected version {product.version} — store as{" "}
          <code>{baseUrl.replaceAll(product.version, "{version}")}</code>
        </label>
      )}
```

In `handleCreate`, when the checkbox is on and applicable, include `url_template` in the create payload:

```tsx
      const tmpl =
        product.version && templatize && baseUrl.includes(product.version)
          ? baseUrl.replaceAll(product.version, "{version}")
          : undefined;
      await createSource({
        product_id: product.id,
        name: name.trim(),
        base_url: baseUrl.trim(),
        ...(tmpl ? { url_template: tmpl } : {}),
      });
```

- [ ] **Step 2: Per-source edit affordance**

In the per-source row (the `SourceItem` component), add a small control to set/clear the template using the server-confirmed detector. When clicked with the product versioned:

```tsx
      // set: detect from the stored base_url
      const { url_template } = await detectVersionToken(source.id, productVersion);
      if (url_template) await updateSource(source.id, { url_template });
      // clear:
      await updateSource(source.id, { url_template: null });
```

Thread the product's `version` into `SourceItem` as a `productVersion?: string | null` prop (same pattern as the existing `platformOptions` prop). Show the source's current `url_template` (read-only) next to its base URL when set. Call the existing per-source refresh (`onSourceChanged`) after the update.

- [ ] **Step 3: Build + lint + manual check**

Run: `cd frontend && npm run build && npm run lint 2>&1 | tail -1`
Expected: build succeeds; lint ≤ baseline.
Manual: on a versioned product, typing a URL containing the current version shows the templatize checkbox + live preview; creating the source stores the `{version}` template; the per-source control sets/clears the template.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/SourceList.tsx
git commit -m "feat(ui): auto-detect {version} templating on the source URL input"
```

---

### Task 6: Changelog + version-history labeling

**Files:**
- Modify: `frontend/src/components/VersionOverlay.tsx` (per-article version list/timeline)
- Modify: `frontend/src/components/ChangelogPanel.tsx` (consolidated changelog)

**Interfaces:**
- Consumes: `ArticleVersion.version`, `ChangelogEntry.version` (Tasks 2–3).

- [ ] **Step 1: Version tags + boundary divider in `VersionOverlay.tsx`**

Read `VersionOverlay.tsx` (it renders the article's `versions` list). For each rendered snapshot, when `version` is non-null, show a tag (e.g. `<span className="version-tag">v{v.version}</span>`). Between two consecutive rendered snapshots whose `version` differ and both are non-null, render a divider:

```tsx
      {prev && cur.version && prev.version && cur.version !== prev.version && (
        <li className="version-boundary">{cur.version} → {prev.version}</li>
      )}
```

(The list is newest-first, so the boundary reads `newer → older` per adjacency; adjust the label order to match the list direction the component renders. A snapshot with `version === null` gets no tag and participates in no divider.)

- [ ] **Step 2: Version tags + boundary divider in `ChangelogPanel.tsx`**

Read `ChangelogPanel.tsx` (it renders `entries`, typically grouped by date). For each entry with a non-null `version`, show the same `version-tag`. Where consecutive entries (or date-groups) cross a non-null version change, render a `version-boundary` divider with the `{older} → {newer}` (or matching the panel's ordering) label. Entries with `version === null` (pre-versioning history) get no tag/divider.

- [ ] **Step 3: Minimal styling**

Add small styles for `.version-tag`, `.version-boundary`, `.version-bar`, `.version-badge`, `.bump-preview`, `.templatize-hint` to the existing stylesheet (find where component styles live, e.g. `frontend/src/index.css` or a component CSS). Keep them consistent with existing classes (reuse `.muted`, badge styles where present).

- [ ] **Step 4: Build + lint + manual check**

Run: `cd frontend && npm run build && npm run lint 2>&1 | tail -1`
Expected: build succeeds; lint ≤ baseline.
Manual: an article with snapshots across a bump shows version tags and a `10.0 → 11.0` divider; the consolidated changelog shows the same; pre-versioning snapshots are untagged.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/VersionOverlay.tsx frontend/src/components/ChangelogPanel.tsx frontend/src/index.css
git commit -m "feat(ui): version tags + boundary markers in history and changelog"
```

---

## Self-review notes

- **Spec coverage:** backend response additions → Tasks 1–2; types/client → Task 3; version bar + bump (Sources view header, client-side preview) → Task 4; auto-detect source templating → Task 5; changelog tag + boundary marker → Task 6. All locked decisions are covered.
- **Type consistency:** `bumpProductVersion`/`enableProductVersioning`/`detectVersionToken` and the `version`/`previous_version`/`url_template` fields are named identically across tasks.
- **Testing reality:** Tasks 1–2 are TDD with pytest; Tasks 3–6 gate on `npm run build` + `npm run lint` (no-regression vs the captured baseline) + manual click-through, because the frontend has no unit-test framework (stated in the spec).
- **Out of scope:** bump/enable/detect backend *logic* (PR #60); auto-version discovery; per-version browsable snapshots.
