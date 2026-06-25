# Versioned-URL Handling — Frontend Design

**Date:** 2026-06-25
**Status:** Approved (design); pending implementation plan
**Goal addressed:** Surface the versioned-URL backend (PR #60) in the UI: let an operator enable versioning on a product, bump its version (rewriting all child source URLs and re-extracting), template a source URL with `{version}`, and see which product version each history snapshot belongs to.

**Backend dependency:** PR #60 (merged) — `Product.version`/`previous_version`, `Source.url_template`, `ExtractionRun.version`, `Article.topic_key`, and the routes `POST /api/products/{id}/versions/bump`, `POST /api/products/{id}/versions/enable`, `POST /api/sources/{id}/detect-version-token`.

## Summary

The product version is managed from a **version bar in the Sources view header** (the view that already lists the product's sources). From there an operator enables versioning or bumps the version; the bump previews the affected source URLs client-side and then calls the backend. Adding/editing a source under a versioned product offers **auto-detect-with-confirm** templating of the URL. The changelog/history **labels each snapshot with its product version** and draws a `10.0 → 11.0` boundary marker where the version changes.

## Decisions (locked during brainstorming)

1. **Control placement:** a version bar atop the Sources view (not the products list).
2. **Source URL input:** paste a plain URL; auto-detect the version token with a confirm toggle + live resolved-URL preview (manual `{version}` typing also works).
3. **Changelog labeling:** version tag on each snapshot **and** a `10.0 → 11.0` boundary divider, in both the per-article timeline and the consolidated changelog.

## Backend response additions (data already exists — expose it)

These are the only backend changes; all are "surface an existing column", with pytest coverage via the existing harness.

| Schema | Field | Source |
|---|---|---|
| `ProductResponse` | `version`, `previous_version` | `Product` columns |
| `ArticleVersionResponse` | `version` | `ExtractionRun.version` via `extraction_run_id` (join in the versions query) |
| `ChangelogEntry` | `version` | `ExtractionRun.version` via the entry's run (join in the changelog query) |

No new backend *logic*; bump/enable/detect already shipped in PR #60.

## Frontend architecture

State stays in `App.tsx` (`selectedProduct`) as today; new components are leaf components that take the product/sources as props and call the API client.

### Types (`src/types/index.ts`)
- `Product`: add `version: string | null`, `previous_version: string | null`.
- `ArticleVersion`: add `version: string | null`.
- `ChangelogEntry`: add `version: string | null`.

### API client (`src/api/client.ts`)
- `bumpProductVersion(productId: string, version: string): Promise<{ version: string; runs: string[] }>` → `POST /products/{id}/versions/bump`.
- `enableProductVersioning(productId: string, version: string): Promise<{ version: string; templatized_sources: number }>` → `POST /products/{id}/versions/enable`.
- `detectVersionToken(sourceId: string, version: string): Promise<{ url_template: string | null }>` → `POST /sources/{id}/detect-version-token`.

### `ProductVersionBar` (new component) — rendered in the Sources view header
- **Not versioned** (`product.version == null`): an **"Enable versioning"** button opening a small inline form that asks the current version, calls `enableProductVersioning`, then refreshes the product + sources.
- **Versioned**: shows `Version: {product.version}` and a **"Bump version"** button opening `BumpVersionModal`.
- Receives the product and the current source list (for the bump preview) as props; calls back to refresh on success.

### `BumpVersionModal` (new component)
- Input: the new version string.
- **Preview (client-side):** for each source with a `url_template`, show `old base_url → url_template.replace("{version}", newVersion)`. Sources without a template are listed as "unaffected". No backend call for the preview.
- Confirm → `bumpProductVersion`; on success close, refresh sources (their rows already poll run status), and surface the returned run count.
- Disable confirm when the new version is empty or equals the current version (mirrors the backend's 400s).

### Source URL input (auto-detect with confirm) — `SourceList.tsx`
- **Add-source form** (no source exists yet, so detection is **client-side**): keep the plain URL field. When `product.version` is set and the entered URL contains that version string, show an inline toggle **"Detected version {v} → store as `{version}` template?"** with a live preview of the resolved URL. The template is computed client-side as `url.replaceAll(product.version, "{version}")` (the first occurrence is what matters; the backend re-derives `topic_key` from the persisted URL regardless). On submit with the toggle on, send `url_template` in the create payload; otherwise send a plain `base_url` as today.
- **Per-source edit** (source exists): the same affordance for setting/clearing `url_template`, here backed by `detectVersionToken(source.id, product.version)` for a server-confirmed template (the source's stored `base_url` is the input). Clearing the toggle sends `url_template: null`.

### Changelog labeling — `VersionOverlay.tsx` / version list, and `ChangelogPanel.tsx`
- Each snapshot/entry shows a version tag (e.g. `v10.0`) from the new `version` field.
- A `{prev} → {curr}` divider renders between two consecutive snapshots (timeline) or date-groups (changelog) whose `version` differs and both are non-null. When a snapshot's `version` is null (pre-versioning history), no tag/divider is drawn for it.

## Data flow

1. Operator opens a product's Sources view → `ProductVersionBar` reads `product.version`.
2. Enable → `enableProductVersioning` → backend templatizes + re-keys → refresh.
3. Bump → modal previews client-side → `bumpProductVersion` → backend rewrites URLs + enqueues runs → source rows show the new runs progressing.
4. Add source → optional auto-detect templating → `createSource({ url_template })`.
5. History/changelog → version tags + boundary dividers from the `version` field now present on the responses.

## Error handling

- Bump/enable failures surface the backend error message inline in the bar/modal (same pattern as existing `SourceList` error handling); the modal stays open so the operator can retry.
- The bump confirm is disabled for empty/same version (the backend also enforces 400).
- Auto-detect toggle is best-effort: if detection finds nothing, the source is created as a plain URL.

## Testing

The frontend has no unit-test framework; the project gates on `npm run build` (type-check) and `npm run lint`. Each frontend task's gate is therefore: **build clean, lint clean, and a manual click-through** of the new control. The backend response additions are covered by the existing pytest harness (assert the new fields appear and carry the joined version).

## Out of scope

- Any new bump/enable/detect backend *logic* (shipped in PR #60).
- Auto-discovery of new versions from vendors.
- Browsable per-version snapshots (history is the continuous per-topic timeline).
