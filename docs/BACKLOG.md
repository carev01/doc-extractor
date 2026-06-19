# Backlog

Future work items, most recent first. Each item is self-contained enough to be
promoted to a spec/plan when picked up.

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
