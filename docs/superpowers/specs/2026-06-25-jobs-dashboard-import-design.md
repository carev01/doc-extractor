# Design: Job/source management improvements

Date: 2026-06-25

Four independent improvements to DocExtractor's job, source, and monitoring UX:

1. Correct progress tracking on incremental runs.
2. A source picker in the job view (assign sources *from* the job).
3. A health-first dashboard for source extraction status.
4. Bulk import of sources via CSV.

Each is self-contained; they share only the routing/registration plumbing.

---

## Feature 1 — Correct progress tracking on incremental runs

### Problem

`pctOf()` in `frontend/src/components/JobsView.tsx` computes progress as
`articles_extracted / articles_total`. But `articles_extracted` only counts
**new** pages (see `firecrawl.py` — `articles_extracted` is incremented only for
the `"new"` outcome; `"updated"` bumps `articles_updated`, unchanged pages bump
`articles_unchanged`). On an incremental run almost every page is unchanged or
updated, so the bar reads near-zero and is useless for tracking progress.

A second, backend-side defect compounds this. On resume, the service seeds the
counter directly:

- `firecrawl.py:~950` (raw-HTTP content path): `articles_extracted = completed`
- `firecrawl.py:~1411` (Firecrawl batch path): `run.articles_extracted = resumed`

This lumps every resumed (already-done) page into the **new** bucket, corrupting
the new/updated/unchanged breakdown shown in the Recent list and RunDetail, and
still does not make a *fresh* incremental run's bar correct.

### Definition

"Processed" pages =
`articles_extracted + articles_updated + articles_unchanged + articles_resumed`,
measured against `articles_total`.

`articles_resumed` is a new counter holding the number of pages carried over
from a prior interrupted attempt (recorded in the resume checkpoint). It is
counted toward progress but kept out of the new/updated/unchanged breakdown so
that breakdown stays semantically clean.

### Backend changes

- **Model:** add `articles_resumed: Mapped[int]` to
  `app/models/extraction_run.py` (`Integer, default=0, server_default="0",
  nullable=False`).
- **Migration:** new Alembic revision adding `articles_resumed` to
  `extraction_runs` with server default `0`.
- **Service (`app/services/firecrawl.py`):** at both resume points, set
  `articles_resumed = <resumed/completed count>` instead of assigning to
  `articles_extracted`. Leave `articles_extracted`/`updated`/`unchanged` to be
  incremented only by actual per-page processing this attempt.
  - Audit the "blocked/empty run" guard near `firecrawl.py:~1460`
    (`persisted = extracted + updated + unchanged`): include `+ resumed` so a
    fully-resumed run that processes nothing new this attempt is not
    mis-flagged as a blocked/zero run.
- **Route (`app/routes/extraction.py`):** add `"articles_resumed":
  r.articles_resumed` to each run dict in `list_runs` and to
  `get_run_status`.

### Frontend changes (`frontend/src/components/JobsView.tsx`)

- Add a helper `processed(run)` returning
  `(articles_extracted + articles_updated + articles_unchanged + articles_resumed)`.
- `pctOf(run)` uses `processed(run) / articles_total`.
- Active list and RunDetail "Processed / total" use `processed(run)` instead of
  `articles_extracted`.
- RunDetail keeps the separate New / Updated / Unchanged stat lines (now
  accurate because resumed pages no longer leak into "New"); add a
  "Carried over" stat line showing `articles_resumed` when `> 0`.
- `frontend/src/types/index.ts`: add `articles_resumed: number` to the
  `ExtractionRun` type.

### Tests

- A run-progress unit/integration test asserting that for a run with
  e.g. `extracted=2, updated=3, unchanged=10, resumed=5, total=20` the API
  returns all fields and a computed-percentage helper (frontend) yields 100%.
- Backend: a test for the resume path confirming resumed pages land in
  `articles_resumed`, not `articles_extracted`, and the blocked-run guard
  accounts for resumed pages.

---

## Feature 2 — Source picker in the job view

### Problem

Today a source is assigned to a job only from the product's source list
(`PUT /api/jobs/{job_id}/sources/{source_id}`). The user wants the inverse:
pick sources from the job card.

### Backend changes

- **New endpoint `GET /api/sources/pickable`** (in `app/routes/sources.py`):
  returns every source with display labels and current assignment:
  ```json
  { "sources": [
      { "id", "name", "vendor_name", "product_name",
        "job_id", "job_name" }
  ] }
  ```
  Implemented with the same Source→Product→Vendor join already used in
  `jobs._job_sources`, left-joined to `jobs` for `job_name`. Ordered by
  vendor, product, name.
- **New bulk-assign endpoint `PUT /api/jobs/{job_id}/sources`** (in
  `app/routes/jobs.py`): body `{ "source_ids": [uuid, ...] }`. Sets
  `job_id` on each listed source (assign or reassign; respects one-job-per-source
  by simply overwriting). Returns the updated `JobResponse`. 404 if the job or
  any source id does not exist. The existing single-source
  `PUT/DELETE .../sources/{source_id}` endpoints remain unchanged.
- **Schema:** add `JobSourcesAssign(BaseModel)` with `source_ids: list[uuid.UUID]`
  in `app/schemas/job.py`.

### Frontend changes

- **New component `frontend/src/components/SourcePicker.tsx`:** a modal/panel
  taking the current `job` and `onAssigned` callback. Fetches
  `GET /api/sources/pickable`, renders a text filter + a multi-select list of
  `vendor › product › name`. Rows already on another job show
  `(in: <job_name>)`; rows already on *this* job are shown checked/disabled or
  hidden. "Assign selected" calls the bulk endpoint then `onAssigned`.
- **`JobCard` (`JobsManager.tsx`):** add an **"Add sources"** button next to
  "Run now" that opens the picker. On assign, call the existing `onChanged`
  refresh.
- **`client.ts`:** add `listPickableSources()` and
  `assignSourcesToJob(jobId, sourceIds)`.
- **`types/index.ts`:** add a `PickableSource` type.

### Tests

- Backend: `GET /api/sources/pickable` returns labels + current job; bulk
  assign assigns/reassigns and is idempotent; bad source id → 404.

---

## Feature 3 — Dashboard (new top-level view, health-first)

### Problem

There is no single place to see which sources have been extracted, which never
have, and how stale each is.

### Backend changes

- **New router `app/routes/dashboard.py`**, registered in `app/main.py`,
  prefix `/api/dashboard`.
- **`GET /api/dashboard/sources`** returns:
  ```json
  {
    "summary": {
      "total": N, "never_extracted": N, "stale": N,
      "failing": N, "running": N
    },
    "sources": [
      { "id", "name", "vendor_name", "product_name",
        "status",                       // source.status
        "last_extracted_at",            // source.last_extracted_at
        "age_seconds",                  // now - last_extracted_at, null if never
        "article_count",                // count of non-removed articles
        "last_run_status",              // latest ExtractionRun.status, null if none
        "last_run_new", "last_run_updated", "last_run_unchanged",
        "job_id", "job_name",
        "next_run_at"                   // from assigned job, if scheduled
      }
    ]
  }
  ```
  - `stale` threshold: query param `stale_days` (default 30). A source is stale
    when `last_extracted_at` is older than the threshold; never-extracted sources
    are counted under `never_extracted`, not `stale`.
  - `failing` = sources whose `status == FAILED` (the source status is set to
    `FAILED` when its extraction fails). `last_run_status` is reported
    separately for display but is not what drives the `failing` count.
  - `article_count` excludes removed articles (`removed_at IS NULL`).
  - Built with aggregate queries (counts grouped by source; latest run via a
    correlated subquery or window function) to avoid N+1.
- **Schema:** `app/schemas/dashboard.py` with `DashboardSummary`,
  `DashboardSourceRow`, `DashboardResponse`.

### Frontend changes

- **New component `frontend/src/components/Dashboard.tsx`:** summary tiles
  (Total / Never extracted / Stale / Failing / Running) above a sortable table.
  Default sort surfaces problems first: never-extracted → failing → stale →
  rest. Columns: Source (vendor › product › name), Status, Last extracted
  (relative age), Articles, Last run result, Job. Clicking a row selects that
  source and navigates to its Browse view.
- **`App.tsx`:** add `"dashboard"` to the `View` union and a **"Dashboard"**
  nav button alongside Vendors / Jobs. Selecting a source row reuses the
  existing `setSelectedSource` + `setView("browse")` flow (Dashboard receives
  an `onSelectSource` callback; it must look up/produce a
  `DocumentationSource`-shaped object — fetch the full source via the existing
  `GET /api/sources/{id}` on click).
- **`client.ts`:** add `getDashboard(staleDays?)`.
- **`types/index.ts`:** add `DashboardSummary`, `DashboardSourceRow`,
  `DashboardResponse`.

### Tests

- Backend: dashboard endpoint returns correct summary counts and per-source
  rows for a fixture with extracted / never-extracted / stale / failing sources;
  `stale_days` param respected; `article_count` excludes removed articles.

---

## Feature 4 — Bulk import of sources (CSV)

### Problem

Adding sources one at a time is slow. The user wants to import many at once via
CSV, auto-creating vendors/products as needed.

### CSV format

Header row required. Columns:

| column        | required | notes                                  |
|---------------|----------|----------------------------------------|
| `vendor`      | yes      | matched by name; created if missing    |
| `product`     | yes      | matched by name within vendor; created |
| `source_name` | yes      | source display name                    |
| `base_url`    | yes      | source base URL                        |
| `url_template`| no       | optional `{version}` template          |

### Backend changes

- **New endpoint `POST /api/sources/import`** (in `app/routes/sources.py`):
  body `{ "csv": "<raw csv text>" }` (JSON-wrapped string; keeps it simple and
  testable, no multipart). Parsed with the stdlib `csv` module.
  - For each row: find-or-create vendor by name; find-or-create product by
    (vendor, name); create the source unless one with the same
    `(product_id, base_url)` already exists (then skip as duplicate).
  - Vendors/products are matched case-insensitively on trimmed name to avoid
    accidental duplicates.
  - Returns a per-row result summary:
    ```json
    {
      "created": N, "skipped": N, "errors": N,
      "rows": [
        { "row": 2, "result": "created" | "skipped" | "error",
          "vendor", "product", "source_name", "message" }
      ]
    }
    ```
  - Validation errors (missing required column/value, malformed CSV) produce a
    per-row `error` result; the whole import does not abort on one bad row.
    A structurally invalid CSV (no header / unparseable) → 422.
  - All successful rows committed in one transaction at the end.
- **Schema:** `SourceImportRequest` (`csv: str`) and
  `SourceImportResult` / `SourceImportRow` in `app/schemas/source.py`.

### Frontend changes

- **New component `frontend/src/components/BulkImport.tsx`:** a panel with a
  textarea (paste CSV) and a file input (`.csv` → read text client-side into the
  same textarea), a parsed-preview table with basic client-side validation
  (header present, required cells non-empty), then "Import" which posts the raw
  CSV and shows the returned per-row summary.
- **Placement:** an **"Import CSV"** button on the Dashboard view header (and it
  can be reused on the Sources view later). On success, trigger a dashboard
  refresh.
- **`client.ts`:** add `importSources(csvText)`.
- **`types/index.ts`:** add `SourceImportResult` / `SourceImportRow`.

### Tests

- Backend: import creates vendors/products/sources; reuses existing vendor and
  product by name; skips duplicate `(product, base_url)`; bad row recorded as
  error without aborting; malformed CSV → 422.

---

## Cross-cutting / non-goals

- **Routing:** register the new `dashboard` router in `app/main.py`
  `include_router` block.
- **Migrations:** one new Alembic revision (Feature 1's `articles_resumed`
  column). Features 2–4 add no new tables/columns.
- **Testing convention:** all backend tests use the existing synchronous
  `psycopg2` + `Session` pattern under `tests/`.
- **Non-goals:** no change to the extraction engine logic beyond the resume
  counter fix; no scheduling-model changes; no auth; CSV import is name-based
  matching only (no fuzzy matching, no update-existing-source semantics — it
  creates or skips).
