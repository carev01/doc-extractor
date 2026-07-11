# Design — Dashboard Overhaul (attention-first) + PDF-Escalation Stats

- **Date:** 2026-07-11
- **Status:** Approved (design); implementation pending
- **Builds on:** the existing dashboard (`/api/dashboard/sources`, `Dashboard.tsx`), the image-enrichment stats (`/api/dashboard/enrichment`), and the PDF VLM escalation retry (`ExtractionRun.escalation_pending`, `POST /api/extraction/runs/{run_id}/retry-escalation`).

## Purpose

Two goals: (1) surface **PDF-escalation** failure/pending stats on the Dashboard (today they live only in the per-job view), and (2) an **attention-first overhaul** of the Dashboard with real sorting, filtering, and in-context drill-down. The unifying idea: the Dashboard becomes **one filterable source table driven by a small set of health "dimensions,"** with the summary tiles as the primary filter controls and a side-panel drill-down. Escalation and enrichment become two of those dimensions rather than bolted-on blocks.

## Decisions (settled during brainstorming)

- **Attention-first unified redesign** (not incremental, no trend charts yet): tiles-as-filters + faceted filters + search + sortable columns + a flags column + an in-context drill-down panel.
- **One consolidated endpoint**: `GET /api/dashboard/overview` returns everything per source + aggregates in one payload; the Dashboard does all sorting/filtering/drill-down **client-side** (≈280 sources is trivial in-memory).
- **In-context side panel** for drill-down (keeps the dashboard + current filters visible), with inline **Describe missing images** and **Retry escalation** actions. A "full source view" link remains.

## Health dimensions

| Dimension | Per-source signal | "Needs attention" |
|---|---|---|
| Freshness | `last_extracted_at` age vs `stale_days` | never extracted / stale |
| Extraction | `source.status` + latest run | failed / running |
| Image enrichment | `described` / `pending` images | `pending > 0` |
| PDF escalation | latest run's `escalation_pending` | warning (count > 0) |

## Backend

### 1. Escalation signal (per source)

A source has an escalation warning when its **latest run** has a non-empty `escalation_pending` (JSONB list of failed segments). Expose, per source: `escalation_warning: bool`, `escalation_pending_count: int` (= `len(escalation_pending)`), and `escalation_run_id: uuid | None` (the run to retry). Derived from the same "latest run per source" `DISTINCT ON` the dashboard already computes — no new query shape, just read `escalation_pending` off that run. Only meaningful for `source_type == "pdf"`.

### 2. `GET /api/dashboard/overview`

A superset of `/api/dashboard/sources`, RBAC-filtered by `visible_vendor_ids()`. Per-source row:

```
id, name, vendor, product, source_type,
status, last_extracted_at, age_seconds, article_count,
last_run: { status, new, updated, unchanged, run_id } | null,
enrichment: { described, pending },
escalation: { warning, pending_count, run_id },
active_run: bool,
job: { id, name, next_run_at } | null
```

Aggregates block (all respect the RBAC scope):
```
total, never_extracted, stale, failing, running,
enrichment: { described, pending, sources_with_backlog },
escalation: { sources_with_warning }
```

- **Enrichment** counts reuse the exact query/predicate from `/api/dashboard/enrichment` (`described = description IS NOT NULL`; `pending = description IS NULL AND is_meaningful IS NOT FALSE`), joined per source. Compute once and attach.
- `active_run` from the PENDING/RUNNING/PAUSED subquery.
- The existing `/api/dashboard/sources` and `/api/dashboard/enrichment` **remain** (the latter still backs the `SourceList` per-source badge); `overview` is additive and becomes the Dashboard's single source. (A later cleanup can retire `/sources` once nothing else uses it — out of scope here.)

### 3. Response schemas

Add `DashboardOverviewResponse` + nested `OverviewSourceRow`, `OverviewLastRun`, `OverviewEnrichment`, `OverviewEscalation`, `OverviewAggregate` to `app/schemas/dashboard.py`.

## Frontend (`Dashboard.tsx` + supporting files)

### 4. Data + state

`getDashboardOverview()` in `api/client.ts` → one fetch on mount + a 20s poll (the pattern we just added for enrichment). All view state (search text, active facets, sort key/dir, selected drill-down source) lives in the component; the visible rows are a `useMemo` over the fetched rows + filters + sort.

### 5. Filter/sort shell

- **Tiles as filters** — the summary tiles (`Sources`, `Stale`, `Failing`, `Running`, `Enrichment backlog`, `Escalation`) become **toggle filters**: clicking a tile filters the table to that dimension; the active tile is highlighted; clicking again clears it. Tile counts reflect the *unfiltered* totals (so they stay a stable overview), while the table reflects the composed filters.
- **Facets** — `Vendor` (multi), `Source type` (web/pdf), `Status`, and a `Flags` multi-select (`never`, `stale`, `failed`, `enrichment-backlog`, `escalation-warning`). Facets compose (AND across facet types, OR within a facet).
- **Search** — free-text over `vendor / product / name`.
- **Sort** — clickable column headers (Source, Type, Freshness, Articles, Last run, Pending images, Escalation), ascending/descending; default remains attention-first (never → failed → stale → escalation → rest, then name) but is now overridable.
- **Flags column** — compact badges per row: `stale`, `failed`, `🖼N` (pending images), `⚠pdf(N)` (escalation), `▶` (active run).

### 6. Drill-down side panel

Row-click opens a right-side panel (dashboard + filters stay visible; ESC/backdrop closes):
- **Header**: vendor › product › name, status, type; a "Open full source view" link (existing navigation).
- **Runs**: the source's recent runs (reuse the runs list shape; link to logs).
- **Image enrichment**: `described / pending`, and a **Describe missing images** button (calls `enrichSource`, disabled when `active_run` or `pending == 0`) — same behavior as the SourceList action.
- **PDF escalation** (pdf sources with a warning): the count of failed segments + a **Retry escalation** button (`POST /api/extraction/runs/{escalation_run_id}/retry-escalation`), disabled when a run is active.
- Any action refreshes the overview (re-poll) so tiles/table/panel stay consistent.

### 7. Layout

```
Dashboard                                            [ search sources… ]
[Sources][Stale ⚠][Failing ⛔][Running ▶][Enrich 🖼][Escalation ⚠pdf]   ← toggle-filter tiles
Filters: [Vendor ▾][Type ▾][Status ▾][Flags ▾]                 Sort:[col ▾]
┌ table (sortable headers, Flags column) ─────────────┬ drill-down panel ┐
│ …rows…                                              │ (on row-click)   │
└─────────────────────────────────────────────────────┴──────────────────┘
```

Reuse existing tile/table/badge styles; add minimal CSS for the filter bar, the panel, and active-tile state. Responsive: the panel collapses to a full-width overlay on narrow viewports.

## Phasing (each independently shippable, and the suggested task order)

1. **Backend**: escalation signal + `/api/dashboard/overview` (+ schemas, tests).
2. **Frontend data swap**: Dashboard fetches `overview`, renders the same table from the new shape (no behavior change yet) — de-risks the data migration.
3. **Filter/sort shell**: tiles-as-filters, facets, search, sortable columns, flags column.
4. **Drill-down panel**: runs + enrichment(+action) + escalation(+retry), with refresh-on-action.
5. **Polish**: empty/loading states, responsive panel, active-filter summary, keyboard (ESC).

## Error handling

- Overview endpoint: RBAC-empty → empty payload; sources with no images/no pdf → zeroed enrichment/escalation (not absent).
- Panel actions surface the server `detail` (via the existing `apiError` helper); 409s (feature disabled / nothing to do / active run) show inline.
- Poll failures are silent (keep the last good data).

## Testing

- **Backend**: overview row correctness (status/freshness/articles/last-run), escalation signal (latest run's `escalation_pending` → warning/count/run_id; non-pdf → no warning), enrichment counts folded in and equal to `/dashboard/enrichment`, aggregates, RBAC scoping. (TDD, `./venv/bin/pytest`.)
- **Frontend**: no test suite → verify via `npm run lint` + `npm run build`; manual pass on filter composition, tile-toggle, sort, and the two panel actions. (Where a pure helper is extracted — e.g. a `filterAndSort(rows, filters, sort)` function — it can carry a lightweight unit check if a test runner is introduced; otherwise keep it pure and obvious.)

## Out of scope (this spec)

- Trend/analytics charts (a later, separate surface).
- Retiring `/api/dashboard/sources` (kept until nothing consumes it).
- Repointing the `SourceList` badge off `/api/dashboard/enrichment` (that endpoint stays).
- Bulk actions across selected sources (possible follow-up once the table has selection).
