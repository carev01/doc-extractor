# Backlog

Future work items, most recent first. Each item is self-contained enough to be
promoted to a spec/plan when picked up.

---

## Product layer: group documentation URLs under a product (Vendor → Product → Source)

**Status:** Open · **Priority:** High (data model) · **Filed:** 2026-06-23

### Problem

The data model is two levels: `vendors` → `documentation_sources`, where each
source is a single `base_url` (`app/models/vendor.py`, `app/models/source.py`).
A real product's documentation routinely spans **multiple URLs**, so today it
can't be represented as one thing:

- Afi.ai publishes one product family across separate trees — `/docs/platform/`,
  `/docs/k8s/`, `/docs/o365/`, `/docs/api/`, … — each of which has to be added as
  its own top-level source hung directly off the vendor.
- Other vendors split a single product's docs into many guide URLs (admin guide,
  install guide, release notes, …).

Because product identity is conflated with a single URL, there is no way to see
"all documentation for product X", to export/browse a product as a unit, or to
group related sources. The vendor list flattens unrelated trees together.

### Desired behavior

A three-level hierarchy **Vendor → Product → Source(s)**. A product belongs to a
vendor and groups 1..N documentation sources (each still its own `base_url`,
TOC, runs, and versions). Browse / export / changelog can eventually operate at
**product scope** (aggregate across the product's sources) as well as per-source.

### Likely approach / where to look

- **Schema:** new `products` table (`id`, `vendor_id` FK, `name`, timestamps);
  add nullable `product_id` FK to `documentation_sources`. Register the model in
  `app/models/__init__.py` (see the "models imported before create_all"
  invariant in CLAUDE.md). Alembic migration + data backfill.
- **Backfill:** for each existing source, create a product (named after the
  source) and link it — or a single "Default" product per vendor; decide below.
- **API:** products CRUD nested under a vendor (`app/routes/`), and let
  `documentation_sources` be created/moved under a product.
- **UI:** insert a Product level between the `vendors` and `sources` views in
  `App.tsx` (vendors → products → sources).

### Open decisions (confirm when picked up)

- Is `product_id` mandatory long-term (every source belongs to a product) or
  optional (loose sources allowed)? Proposed: mandatory after backfill.
- Backfill strategy: one product per existing source (preserves current list) vs
  one "Default" product per vendor (flatter). Proposed: one product per source,
  named after the source, so nothing visually disappears.
- Whether export/browse/changelog aggregate at product scope in v1 or stay
  per-source initially (product is just a grouping at first).

### Done when

- Schema + migration land with existing vendors/sources/articles preserved.
- A user can create products under a vendor and assign multiple sources to one
  product; the UI navigates Vendor → Product → Source.
- Per-source extraction/browse/export still work unchanged under the new nesting.

---

## Enable renaming vendors, products, and documentation (bookshelf) names

**Status:** Open · **Priority:** Medium · **Filed:** 2026-06-23

### Problem

Names are effectively fixed once created. There's no affordance in the UI to
rename a **vendor**, a **product** (once the product layer above exists), or a
**documentation source** ("bookshelf"). The API is already half-there but unused
by the frontend:

- `PATCH /api/vendors/{id}` accepts `name` (`update_vendor`, `VendorUpdate`).
- `PATCH /api/sources/{id}` accepts `name` (`update_source`, `SourceUpdate`).
- The frontend only ever calls `updateSource` to set `platform` — there is no
  rename control anywhere (`SourceList.tsx`, the vendor list).
- Products have no model/endpoint yet (depends on the product-layer item).

### Desired behavior

Inline rename (edit button / editable field) for vendor, product, and source
names in their respective list views, persisted via PATCH, with sensible
validation and conflict handling.

### Likely approach / where to look

- **Backend:** vendor + source rename already supported; add a product `PATCH`
  when the product layer lands. Note `vendors.name` is `unique` — surface a clean
  409/validation message on collision rather than a 500.
- **Frontend:** add rename UI to the vendor list and `SourceList.tsx` (and the
  product list once it exists); reuse the existing `updateVendor`/`updateSource`
  client calls.

### Done when

- A user can rename a vendor, product, and source from the UI and the change
  persists across reload.
- Duplicate vendor names (or any uniqueness constraint) fail gracefully with a
  clear message, not a server error.

### Notes

- Partially unblocked already (vendor/source rename exists at the API); the
  product part depends on the "Product layer" item above.

---

## Dedicated task-monitoring view: progress, logs, pause/cancel

**Status:** Open · **Priority:** Medium-High · **Filed:** 2026-06-23

### Problem

Extraction runs are tracked in `extraction_runs` and processed by the worker via
the queue (`app/worker.py`, `app/services/queue.py`), but there is **no dedicated
monitoring UI**. Run status is only glimpsed per-source; there is no cross-source
activity view, no live progress, no access to logs, and no way to stop a run from
the UI. Operationally this forces `kubectl` (psql + pod logs) — which is exactly
how the recent stuck-run and failure investigations had to be done. Gaps:

- **No aggregate view.** Views are `vendors / sources / browse / export /
  changelog / schedule` (`App.tsx`); none lists runs across sources.
- **No live progress.** `articles_extracted` exists on the run, and resumable
  builds track `content_done` in `toc_checkpoints`, but nothing surfaces a
  "N of M pages" progress indicator while a run is in flight.
- **No log access.** Worker logs live only in the pod; not viewable in-app.
- **No pause/cancel.** The queue can claim and reap stale runs, but there is no
  cooperative cancel or pause — a long/wrong run can only be stopped by killing
  the worker pod (which is what caused the earlier mid-run disruption).

### Desired behavior

A separate "Runs"/"Activity" view listing recent and active runs across all
sources with status, progress, timing, and attempt count; drill-in to a per-run
detail with logs; and controls to **cancel** a queued/running run (and, as a
stretch, **pause/resume**).

### Likely approach / where to look

- **Backend:**
  - Endpoints to list runs (paginated; filter by status/source) and fetch run
    detail. A run-progress figure from `articles_extracted` + TOC total +
    checkpoint `content_done`.
  - **Cancel** = cooperative: a `CANCELLING` status/flag the worker checks at
    batch boundaries in the content loop (`firecrawl.py`), landing the run in a
    terminal `CANCELLED` state; a queued run can be cancelled immediately. Avoid
    hard pod kills.
  - **Logs:** either stream pod logs for the run's worker, or persist structured
    per-run log lines to a table/field so the UI can show them after the fact.
    Decide below.
- **Frontend:** new top-level view with a runs table + detail panel + cancel
  button; poll (or stream) for live status/progress.

### Open decisions (confirm when picked up)

- Cancel mechanism: cooperative flag (preferred, clean) vs forced. Confirm the
  worker's content loop has a safe checkpoint to observe the flag.
- Logs: live pod-log stream vs persisted run-log lines (persisted survives pod
  restarts and is simpler to render; streaming is richer but couples to k8s).
- Is **pause/resume** in scope for v1 or a stretch? It needs a `PAUSED` state and
  worker cooperation beyond cancel; resumable builds already checkpoint, so pause
  ≈ cancel-that-keeps-the-checkpoint.

### Done when

- A dedicated view shows all runs (active + recent) with live progress and
  status; no `kubectl` needed for routine monitoring.
- A running or queued run can be cancelled from the UI and promptly reaches a
  terminal state without killing the worker pod.
- Run logs are viewable in the UI.
- (Stretch) a run can be paused and resumed from its checkpoint.

---

## Backfill: re-sanitize already-stored articles

**Status:** Open · **Priority:** Medium · **Filed:** 2026-06-19

### Problem

Content sanitization (`app/services/sanitize.py`) runs at **write time**, inside
`process_article_result`. But Firecrawl's changeTracking reports unchanged pages
as `"same"`, and that fast-path skips re-storing — so existing articles never get
re-sanitized when the *sanitizer* improves (only when the *source* changes).

Concretely: after the table-form copyright-footer fix (commit `f913986`), a full
Datto re-extraction reported 0 updated / 119 unchanged, so stale pages kept their
old footers. The HOME article (`2b397927…`, `SaaS_Protection_Home.htm`) has no
skin chrome and effectively never changes, so it will keep its boilerplate footer
indefinitely without an explicit re-store.

### Options (decide when picked up)

1. **One-time backfill endpoint** — e.g. `POST /api/extraction/resanitize/{source_id}`
   that loads stored articles, re-applies `sanitize_markdown`, and updates the ones
   that changed. Explicit, re-runnable, keeps the hot path untouched. (Leaning here.)
2. **Auto-heal in the `"same"` path** — compare freshly-sanitized content to stored
   and re-store on difference, so future sanitizer changes propagate on the next run.
   Self-maintaining but touches the change-tracking hot path and creates new
   `ArticleVersion` rows.

### Done when

- A maintainer can heal a source's existing articles without changing the source.
- Re-sanitizing is idempotent (no spurious versions once content is already clean).
- Decision recorded on whether healing creates an `ArticleVersion` (audit trail) or
  updates content in place.

---

## Extraction run counter: `articles_new` under-reports newly-added pages

**Status:** Open · **Priority:** Low (cosmetic) · **Filed:** 2026-06-19

### Problem

When an incremental run discovers and stores brand-new pages (e.g. the Confluence
REST-hierarchy fix grew Barracuda BCCB from 22 → 44 articles), the run summary
does not reflect them: `articles_new` comes back `null` and `articles_extracted`
only counts the re-processed pre-existing pages. Observed on run
`efba75c1-40f5-4d75-8108-d27b750bd107`: TOC = 44, stored articles = 44 (all with
content), yet the run reported `updated=3, unchanged=19, new=null` (= 22, the old
set only).

The stored **data is correct** — this is purely a reporting/counter inaccuracy in
the run record, so the UI's "N articles extracted" figure understates newly-added
pages on hierarchy-changing runs.

### Likely cause / where to look

`process_article_result` / the content-scraping loop in
`backend/app/services/firecrawl.py` — the `articles_new` increment path is not hit
(or not initialised) for pages created during this flow. Confirm whether the
"new" branch increments `ExtractionRun.articles_new`, and why it stays `null`
rather than `0`/`22`.

### Done when

- A run that creates K new articles reports `articles_new == K`.
- `articles_extracted` reconciles with new + updated + unchanged.
- Regression test asserts the counter on a mixed new/updated/unchanged run.

---

## Export file lifecycle: retention/cleanup + persistent access to past exports

**Status:** Phases 1 & 2 DONE (commit `5831c4d`, deployed) · Phase 3 stretch open ·
**Priority:** High (operational risk) · **Filed:** 2026-06-19

> **Implemented 2026-06-19:** scheduler hourly retention sweep (age + size cap,
> `DOCEXTRACTOR_EXPORT_RETENTION_DAYS=7`, `DOCEXTRACTOR_EXPORT_MAX_TOTAL_BYTES=3 GiB`,
> both exposed in the Helm chart); `/api/export/list` enriched with source/format/
> created/expires/size and backed by `export_jobs`; Export page now shows a persistent
> "Recent Exports" list. Decision taken: purge deletes the dir **and** the `export_jobs`
> row (no tombstone). **Still open (Phase 3):** manual delete endpoint/button; object-
> storage backend for export artifacts.

### Problem

Generated exports are written to the `exports` PVC under one directory per export
UUID (`exports/<export_id>/`), containing the `.pdf` or `.md` files (plus images
for markdown). Two gaps, verified in the current code:

1. **No cleanup — files live forever.** There is no retention, TTL, or purge logic
   anywhere in the backend. The `exports` PVC is RWO and small (4.9 Gi). At ~46 MB
   per Satori PDF export, it fills after roughly 100 exports, after which **new
   exports fail (disk full)**. Growth is unbounded and silent.
2. **Past exports are unreachable from the UI.** `ExportPanel` holds the export
   result in React local state, so navigating away from the Export page drops the
   download link — even though the files still exist on disk. An `/api/export/list`
   endpoint exists but (a) the frontend never calls it and (b) it only scans the
   filesystem (no timestamp, source, size, format, or zip info, and orders by random
   UUID rather than recency).

### Desired behavior

- Exports are retained for a bounded, configurable window and then purged
  automatically; the PVC never fills from accumulation.
- A user can leave the Export page and come back (or revisit later) and still see
  and download their recent exports, until they expire.
- Expiry is communicated, not silent: the UI shows when an export was created and
  (ideally) when it will be removed; a request for an expired export returns a clean
  404/410 rather than a confusing empty state.

### Proposed approach (phased)

**Phase 1 — Server-side retention sweep (addresses the disk risk first).**
- Drive retention from the existing `export_jobs` table (it already has
  `created_at` and `export_id`) rather than filesystem mtime — gives reliable age
  and metadata.
- Add a sweep to the existing scheduler tick (`app/services/scheduling.py` `_tick`,
  alongside `reap_stale_runs`): delete `exports/<export_id>/` directories whose job
  `created_at` is older than `DOCEXTRACTOR_EXPORT_RETENTION_DAYS` (new setting,
  **recommended default 7**). Either delete the `export_jobs` row too, or keep the
  row and mark it expired (`result` cleared / a `purged_at` column) so the UI can
  still show "expired" instead of a blank.
- Secondary safety cap: also purge oldest-first if total export size exceeds
  `DOCEXTRACTOR_EXPORT_MAX_TOTAL_BYTES` (recommended default ~3 Gi, below the PVC
  size), so a burst can't fill the disk before the age sweep runs.
- Make the sweep idempotent and tolerant of a missing directory (a job row with no
  dir, or a dir with no row).

**Phase 2 — Persistent access in the UI.**
- Enhance `GET /api/export/list` to return metadata from `export_jobs`
  (created_at, source name, format, status, total size, file list, whether a zip
  exists, expiry time) ordered by `created_at` desc — not a bare filesystem scan.
- Add a "Recent exports" section to `ExportPanel` that loads this list on mount, so
  download links survive navigation. Show created/expires timestamps. Reuse the
  existing per-file and zip download endpoints.
- Expired/missing export → the download endpoints return 404/410 and the UI shows a
  clear "this export has expired" message.

**Phase 3 — Stretch / optional.**
- Manual "delete export" endpoint + UI button so users can purge on demand.
- Longer-term: move export artifacts to object storage (S3-compatible) instead of
  the RWO PVC, which also unblocks scaling backend/worker beyond one node. (Ties
  into the existing "object-storage backend" note in the k8s deployment spec.)

### Open decisions (confirm when picked up)

- Retention period default (proposed 7 days) and whether it's age-based, size-based,
  or both (proposed both, age primary + size as a safety cap).
- After a file is purged, keep the `export_jobs` row as a tombstone (nicer UX:
  "expired") or delete it entirely (simpler).
- Whether to also bound the `media`/images footprint (out of scope here; this item
  is only about `exports/`).

### Acceptance criteria

- [ ] Exports older than the configured retention are removed automatically by the
      scheduler; verified the PVC usage drops after the sweep.
- [ ] A size cap prevents the PVC from filling even under a burst of exports.
- [ ] The Export page lists recent (non-expired) exports with timestamps and working
      download links after navigating away and back.
- [ ] Requesting an expired/purged export returns a clean 404/410 and a clear UI
      message (no silent empty state).
- [ ] Retention window and size cap are configurable via `DOCEXTRACTOR_*` settings.
- [ ] Tests: retention sweep (age + size cap, idempotency, missing-dir tolerance);
      enhanced list endpoint; frontend recent-exports rendering.

### Notes

- Current state confirmed 2026-06-19: `exports` PVC 4.9 Gi / 3% used / 3 export
  dirs; no cleanup code; `/api/export/list` exists but is unused by the frontend.
- Related: the redundant-PDF-zip fix (commit `5454edb`) reduced per-export size, but
  accumulation is still unbounded.
