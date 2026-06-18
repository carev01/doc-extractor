# Read-Path Scaling (Docs Browser) — Design

**Date:** 2026-06-18
**Status:** Approved (design); pending implementation plan
**Goal addressed:** Scale the documentation browser to thousands of articles (Commvault Cloud range). Today `DocsBrowser` renders the entire TOC tree recursively into the DOM, which janks at thousands of nodes, and there is no way to locate a page except scrolling.

This is **sub-project 2** of the scaling effort (sub-project 1, async/bounded export, is merged). A live large-source stress test is the user's to run; this lays the foundation.

## Summary

Make the docs-browser sidebar scale by (1) **virtualizing** the TOC — flatten the currently-visible tree to a flat row array and render only the visible window — and (2) adding a **client-side filter** so users can find a page among thousands. The backend `browse` endpoint is unchanged (its metadata-only payload is tolerable at thousands; lazy-loading is deferred).

## Decisions (locked during brainstorming)

| Decision | Choice | Rationale |
|---|---|---|
| Scope | Frontend virtualization + TOC filter; backend `browse` unchanged | The acute bottleneck is DOM rendering of thousands of nodes; backend payload is metadata-only and tolerable. |
| Virtualization | `@tanstack/react-virtual` (headless, React 19-ready) over a flattened visible-row array | Mature, small, supports the flatten-then-window approach cleanly. |
| Filter | Client-side, case-insensitive substring on title; shows matches + ancestor chain, expanded | Full tree already loaded; no backend round-trip; preserves path context. |
| Backend | No change | Lazy-load / pagination is a much larger change for marginal benefit at thousands; deferred (out of scope). |

## Architecture

The only file with substantive change is `frontend/src/components/DocsBrowser.tsx`, plus small helpers and one dependency. Backend, API, the changelog/export/version views, article selection, and the version overlay are untouched.

### 1. Flatten the visible tree

A pure function turns the nested `BrowseTOCEntry[]` into a flat array of visible rows, honoring the existing collapse state:

```
interface FlatRow { node: BrowseTOCEntry; depth: number; hasChildren: boolean; expanded: boolean; }
flattenVisible(entries, collapsed: Set<id>) -> FlatRow[]   // depth-first; skip children of collapsed nodes
```

This replaces the recursive `renderTree`. Expand/collapse toggles the `collapsed` set, which recomputes the flat array.

### 2. Virtualize

The flat array feeds `@tanstack/react-virtual`'s `useVirtualizer` over a scroll container with a fixed row height. Only the visible window of rows is rendered; each row reuses the existing row UI (caret/toggle, title, `badge-new`/`badge-upd`, active highlight, `onClick → selectArticle`), with left padding = `depth * indent`.

### 3. Filter

A text input above the tree drives a pure filter:

```
filterRows(entries, query) -> { rows: FlatRow[]; }   // when query non-empty
```

When the query is non-empty: compute the set of nodes whose title matches (case-insensitive substring) **plus all their ancestors**; produce a flat, fully-expanded row array of just that subset (so a matching deep page shows with its parent path); the matched substring is highlighted in the row title. When the query is empty, fall back to `flattenVisible(entries, collapsed)`. The "Removed pages" list is filtered by the same query (simple substring on its titles).

`visibleRows` is a single `useMemo` of `(data.entries, collapsed, query)` so render work is recomputed only when those change.

### 4. Keep the rest

Article selection (`selectArticle` → `getArticle`), the breadcrumb header, the version overlay, NEW/UPDATED/REMOVED indicators, and the "Removed pages" section all stay; only the sidebar list's *production and rendering* change.

## Dependency

- Add `@tanstack/react-virtual` to `frontend/package.json`.

## Testing

This project has no frontend test runner, so verification is:
- `npm run build` (TypeScript type-check) and `npm run lint` clean, no new lint errors vs the baseline.
- The flatten and filter helpers are written as **small pure functions** (exported from a `docsTree.ts` helper module) so they are reviewable in isolation and could be unit-tested if a runner is added later.
- **Live check against a synthetic large source:** seed ~3,000 TOC entries + articles for a source in the dev DB, open the docs browser, and confirm (a) the tree renders and scrolls smoothly (only a window of rows in the DOM), (b) expand/collapse works, (c) the filter narrows to matches + ancestors and clearing restores the tree, (d) selecting a page still loads it.

## Out of scope
- Backend `browse` pagination / lazy children-on-expand (deferred; only needed at tens of thousands).
- Virtualizing the changelog timeline or article list (already backend-paginated).
- Server-side search of TOC (filter is client-side over the loaded tree).
- Persisting expand/collapse or filter state across reloads.
