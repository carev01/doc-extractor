# Changelog Timeline (added / changed / removed) — Design

**Date:** 2026-06-17
**Status:** Approved (design); pending implementation plan
**Goal addressed:** "keep a consolidated changelog" (CLAUDE.md) — reshaped into a historical, date-grouped timeline of page-level events (added / changed / removed) with click-through to the article and side-by-side diffs for changes.

## Summary

Today's changelog is a flat, newest-first list of **content changes only** (one entry per `ArticleVersion`). This redesign turns it into a **historical timeline grouped by date**, where each date shows the page-level events that happened then:

> **18 Jun 2026** — *Added* "Getting started", *Changed* "Account connection", *Changed* "Set up", *Removed* "Legacy API"

Every event is clickable:
- **Added** → open the article (rendered).
- **Changed** → open the **side-by-side view with highlighted changes** (the existing version-diff overlay) for that change.
- **Removed** → open the preserved article (the last-seen content).

It also adds the one currently-missing event type — **removals** — which requires recording when a page drops out of the source TOC.

## Decisions (locked during brainstorming)

| Decision | Choice | Rationale |
|---|---|---|
| Removal timing | Detect at extraction; stamp `removed_at`/`removal_run_id` when a page first goes missing, clear on re-add | Accurate event time; freshly removed pages sort to the top; survives re-add/re-remove. |
| Grouping | By **calendar date**, newest date first | Matches the "Date A — events" mental model; needs no per-event run id, so no `created_run_id` column. |
| Event sources | `added` from `articles.created_at`, `changed` from `ArticleVersion`, `removed` from `articles.removed_at` | Two of three already exist in the data; only removals need new columns. |
| Click-through | added→article, changed→side-by-side diff (reuse existing overlay), removed→preserved article | Reuses the version-diff UI already built. |

## Data model

`Article` gains two nullable columns:

| Column | Type | Notes |
|---|---|---|
| `removed_at` | timestamptz, nullable | When the page was first detected missing from the rebuilt TOC. NULL while present. |
| `removal_run_id` | uuid, nullable, FK→`extraction_runs` (`ondelete=SET NULL`) | The run that detected the removal (provenance). |

No `created_run_id` is needed — `added` events use the existing `created_at`, and the timeline groups by date.

**Migration (one revision):** add the two columns. Backfill any *currently* orphaned article (`toc_entry_id IS NULL`) with `removed_at = extracted_at` so pre-existing removals appear immediately (none exist right now after the re-link repair, but it keeps the migration self-consistent). `removal_run_id` is left NULL for backfilled rows.

## Removal detection (in `extract_source`)

At the finalize point — right after `_poll_batch_and_process` returns (all pages processed and re-linked) and before the run is marked `COMPLETED` — two idempotent statements per run:

```sql
-- Newly removed: present last time, gone now, not yet flagged.
UPDATE articles
SET removed_at = now(), removal_run_id = :run_id
WHERE source_id = :sid AND toc_entry_id IS NULL AND removed_at IS NULL;

-- Re-added: back in the TOC -> clear the removal flag.
UPDATE articles
SET removed_at = NULL, removal_run_id = NULL
WHERE source_id = :sid AND toc_entry_id IS NOT NULL AND removed_at IS NOT NULL;
```

Because the first statement only stamps when `removed_at IS NULL`, the removal time stays pinned to first detection across later runs. A page that returns is cleared and can be flagged again if it disappears later. This relies on the re-link fix (unchanged pages re-attach their `toc_entry_id`), so the orphan set is exactly the removed pages.

## Changelog route — timeline of events

`GET /api/sources/{id}/changelog` returns a newest-first list of **events** drawn from three sources via `UNION ALL`, ordered by event timestamp desc, paginated (`skip`/`limit`), with `total` = sum of the three counts:

| Event | `change_type` | timestamp | `version_id` | source |
|---|---|---|---|---|
| Page added | `added` | `articles.created_at` | NULL | `articles` |
| Page changed | `changed` | `article_versions.extracted_at` | the version id | `article_versions` |
| Page removed | `removed` | `articles.removed_at` | NULL | `articles` (where `removed_at IS NOT NULL`) |

Each event carries `article_id`, `title`, `change_type`, `timestamp`, `version_id` (changed only), `has_diff` (changed only). The route stays a single endpoint; the union keeps pagination/count simple.

Note: the initial full extraction yields one `added` event per page on the oldest date — the correct "initial import" entry.

## Schema

`ChangelogEntry` becomes an event:
- `change_type: "added" | "changed" | "removed"` (was implicitly all-changed).
- `version_id: uuid | None` (NULL for added/removed).
- `timestamp: datetime` (the event time; replaces the change-specific `extracted_at` label).
- keep `article_id`, `title`, `extraction_run_id` (the relevant run), `has_diff`.

## Frontend — `ChangelogPanel` as a timeline

Rewrite the panel into a **date-grouped timeline**:
- Backend returns the flat newest-first event list; the panel groups consecutive events by calendar date under a date header.
- Each event row shows a type tag (**ADDED** / **CHANGED** / **REMOVED**, color-coded in the existing design system) and the article title.
- Click behavior:
  - **added** → open the article (rendered markdown view).
  - **changed** → open the existing side-by-side version overlay/diff for that `version_id` (highlighted changes).
  - **removed** → open the preserved article (last-seen content), with the existing "no longer in the TOC" banner.
- TS `ChangelogEntry` type gains `change_type` and nullable `version_id`/`timestamp`.

## Testing

- **Extraction (removal detection):** a run that drops a page stamps `removed_at`/`removal_run_id`; a later run leaves the timestamp pinned (first detection); re-adding the page clears both; a never-removed page stays NULL.
- **Changelog route:** events from all three sources appear; correct newest-first ordering across types; `change_type` and `version_id` correct (version_id only for `changed`); `total` and pagination span the union; a source with only adds (first run) returns N `added` events.
- **Frontend:** verified via `npm run build` + `npm run lint` (no component test runner); manual check that date grouping and the three click-throughs resolve.

## Out of scope
- Grouping by individual run (date grouping chosen); per-run drill-down.
- "Renamed" events (URL change reads as removed+added, per the existing rename decision).
- Reorder/re-parent events (structural-only changes are not timeline events).
- Changelog export/RSS.
