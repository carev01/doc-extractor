# Delta-Feed Deletion Tombstones + Resumable Bootstrap — Design

**Date:** 2026-07-11
**Status:** Approved (design)
**Goal:** Close the two remaining gaps in the delta-feed contract that a downstream GraphRAG consumer relies on: (1) deleting a source/product/vendor must propagate as `removed` tombstones so the consumer can drop those nodes, and (2) the full-corpus bootstrap snapshot must be resumable after a dropped connection without missing updates.

---

## Background

The delta feed (`GET /api/articles/delta`, `app/services/delta_feed.py`) is the pull-based sync channel for downstream consumers. It has two modes: a **bootstrap** snapshot (no `since`) that scans the live `articles` table, and an **incremental** stream (`since=<cursor>`) driven by the append-only `content_changes` outbox. The `extraction_complete` webhook nudges the consumer to pull.

Two gaps were found in the 2026-07-11 downstream-readiness review:

1. **Entity deletion emits no tombstones.** `delete_source`, `delete_product`, and `delete_vendor` do a plain `db.delete(entity)`; the `ON DELETE CASCADE` FK on `articles.source_id` hard-deletes all descendant articles **without writing `removed` rows** to `content_changes`. A consumer that already ingested those articles is never told they're gone → permanent orphan nodes. (Per-run removals during extraction already tombstone correctly via `firecrawl.py` `record_removals`; only out-of-band entity deletion is uncovered.)

2. **Bootstrap is not resumable.** `stream_bootstrap` keyset-paginates internally by `Article.id` but emits its continuation cursor only at the *end*. If the stream drops mid-way (multi-thousand-page corpora exist, e.g. Rubrik ~4240 pages), the consumer must restart the entire snapshot.

## Key facts that ground the design (verified in code)

- `content_changes` FKs are `ON DELETE SET NULL` on `article_id`, `source_id`, `run_id`; `topic_key` is denormalized onto the row (`app/models/content_change.py`).
- `_tombstone_record(change)` (`delta_feed.py:144`) is **fully self-contained**: `id`←`change.article_id`, `topic_key`←`change.topic_key`, `source_id`←`change.source_id`, `removed_at`←`change.created_at`, `run_id`←`change.run_id`. It never reads the `articles` row. So a tombstone survives the article being hard-deleted — the only thing that erases identity today is the FK nulling `article_id`.
- `_safe_ceiling(db)` (`delta_feed.py:77`) is `min(content_changes.id)` **inner-joined** to `extraction_runs` on `run_id` filtered to active statuses. Rows with `run_id IS NULL` never participate in the join, so they never form a ceiling, and they are served once they fall below any active run's floor.
- `record_removals(db, *, rows, source_id, run_id)` (`change_log.py:33`) appends one `removed` `ContentChange` per row; each `row` needs `.id` and `.topic_key`. `run_id` is currently typed `uuid.UUID`.
- `stream_bootstrap` computes its watermark `max_seq = min(max(content_changes.id), ceiling-1)` **at function entry** (correct start-time value) but only yields it in the final control line.

---

## Part 1 — Deletion tombstones (append-only outbox)

### 1.1 Schema change (migration; prerequisite)

Make `content_changes` a true append-only log: **drop the three `ON DELETE SET NULL` FK constraints** (`content_changes.article_id → articles.id`, `content_changes.source_id → documentation_sources.id`, `content_changes.run_id → extraction_runs.id`). Keep each as a plain **nullable UUID column** (no FK). The ORM model loses the `ForeignKey(...)` on these three columns.

Rationale: a historical event row must not be mutated by a later parent delete. After this change, hard-deleting an article/source/run leaves its `content_changes` rows intact with the real ids preserved.

Safety notes:
- `_safe_ceiling`'s join is on value equality against **active** runs (which always exist while active), so dropping the `run_id` FK does not change its result; dangling/NULL `run_id` rows are simply excluded from the join, exactly as before.
- No ORM `relationship()` targets these columns; the feed already tolerates `article=None` for `added`/`updated` rows (it skips stale ones), so a dangling `article_id` is not newly problematic.

Migration is Alembic (`alembic revision --autogenerate` will detect the FK drops; verify the generated `drop_constraint`/`create_foreign_key` pair by name and pin explicit constraint names in the migration). Downgrade re-creates the three FKs with `ondelete="SET NULL"`.

### 1.2 Shared tombstone helper

Add a helper (in `app/services/change_log.py`) that, given a set of source ids, enumerates their **live** (`removed_at IS NULL`) articles as `(id, topic_key, source_id)` and writes one `removed` `ContentChange` per article with `run_id=None`, in the caller's transaction:

```
async def record_source_deletions(db, *, source_ids: Sequence[uuid.UUID]) -> int:
    """Tombstone every live article in the given sources (run_id NULL). Returns count.
    Caller commits. Must run BEFORE the entities are deleted so the articles still exist."""
```

`record_removals`'s `run_id` parameter is widened to `uuid.UUID | None` (the column is already nullable) so the extraction path and this path share it; `record_source_deletions` may either call `record_removals` per source group or write rows directly (implementer's choice, but the row shape and `run_id=None` are fixed).

### 1.3 Deletion routes

In each of `delete_source`, `delete_product`, `delete_vendor` (`app/routes/{sources,products,vendors}.py`): compute the affected source-id set, call `record_source_deletions(db, source_ids=...)` **before** `db.delete(entity)`, then commit once (existing cascade purges articles/images/versions; the outbox rows are untouched because their FKs are gone).

Affected source-id sets:
- `delete_source(source_id)` → `[source_id]`
- `delete_product(product_id)` → sources where `product_id == product_id`
- `delete_vendor(vendor_id)` → sources where the product's `vendor_id == vendor_id`

RBAC is already enforced by the existing `authorize_*` calls at the top of each route; the tombstone step runs after authorization.

### 1.4 Gap-free correctness

The delete is a single atomic transaction: either all its `removed` rows commit or none do. Once committed, they sit below any active run's ceiling (or are withheld together with that run's window until it finishes). No `run_start` sentinel is needed because there is no multi-commit run to protect. A consumer therefore sees either the whole deletion or none of it, never a partial set.

---

## Part 2 — Resumable bootstrap

### 2.1 Server

Add an optional query parameter `bootstrap_after: uuid.UUID | None` to `article_delta_feed`, forwarded to `stream_bootstrap` (meaningful only when `since` is omitted; if both are supplied, `since` wins / `bootstrap_after` is ignored — document it). In `stream_bootstrap`, add `Article.id > bootstrap_after` to the keyset conditions when set.

`stream_bootstrap` additionally yields its watermark as an **initial** control line *before* the article records:

```json
{"control":"bootstrap_start","next_since":"<cursor for X>"}
```

where `X` is the same start-time watermark it already computes. The final completion line is unchanged:

```json
{"control":"cursor","next_since":"<cursor for X>","count":N}
```

(Same `X` in both lines within a single request.)

### 2.2 Consumer contract (documented, not enforced)

- On the **first** bootstrap attempt, store `next_since` from the `bootstrap_start` line immediately.
- Apply article records, tracking the highest `id` applied.
- If the stream drops before the final `{"control":"cursor",...}` line, resume with `?bootstrap_after=<highest id applied>` and **keep the originally-stored `next_since`** (ignore the resumed stream's `bootstrap_start` value, which is recomputed at resume time).
- Only begin incremental (`?since=<stored next_since>`) after receiving the final completion line.

### 2.3 Why the start-watermark matters (the missed-update case)

If the consumer used the *resume-time* watermark, an update to an already-emitted (lower-id) article that occurred between the original start and the resume would fall **below** that watermark and never be served by incremental — a silent stale node. Anchoring the incremental cursor to the **first attempt's start watermark** guarantees every such change is replayed by incremental (idempotent upserts absorb any overlap). This is why the watermark is delivered up front and preserved across resumes.

### 2.4 Backward compatibility

Existing consumers read the last line for `{"control":"cursor",...}`; the new `bootstrap_start` line is an additional, distinctly-typed control record they can ignore. No change to record shapes or the incremental path.

---

## Documentation

Update the `docs/API.md` Delta-Feed section:
- Note that deleting a source/product/vendor now emits `removed` tombstones (with intact `id`), so consumers should process removals to avoid orphans.
- Document `bootstrap_after`, the `bootstrap_start` control line, and the resume contract (capture watermark up front, keep it across resumes, start incremental only after completion).

## Testing (TDD)

Backend (`tests/`, sync `psycopg2`/httpx pattern):

**Deletion tombstones**
- Deleting a source with N live articles writes N `removed` rows carrying the real `article_id` and `topic_key`; the ids are still present after the source (and its articles) are hard-deleted.
- The delta feed serves those tombstones (`change_type:"removed"`, correct `id`) to a consumer polling from a prior cursor.
- Product deletion tombstones articles across all its sources; vendor deletion across all its products' sources.
- A `run_id=NULL` removal committed while an unrelated run is active is withheld until that run's floor clears, then served (safe-ceiling respected).
- Already-`removed_at` articles are not double-tombstoned (only live articles).

**Resumable bootstrap**
- `stream_bootstrap` emits a `bootstrap_start` control line first with the same `next_since` as the final `cursor` line.
- `?bootstrap_after=<id>` returns only articles with `id >` that value; concatenating a split at any boundary yields the same full set exactly once.
- Missed-update guard: emit article A in a first bootstrap chunk; update A (new `content_changes` row); resume with `bootstrap_after` past A; then an incremental pull from the **first** `next_since` still serves A's update.

## Out of scope (follow-up)

`content_changes` retention/pruning. The outbox is already append-only and unbounded today; this change adds deletion rows to the same latent need. A safe prune is time-based below the safe ceiling (the server does not track per-consumer cursors). Not solved here.

## File touch-list

- `alembic/versions/<new>.py` — drop the three FKs (create nullable UUID columns), downgrade re-adds them.
- `app/models/content_change.py` — remove `ForeignKey(...)` from the three columns (plain nullable UUID).
- `app/services/change_log.py` — `record_removals` `run_id: UUID | None`; add `record_source_deletions`.
- `app/routes/sources.py`, `app/routes/products.py`, `app/routes/vendors.py` — tombstone before delete.
- `app/services/delta_feed.py` — `bootstrap_after` filter + `bootstrap_start` control line.
- `app/routes/articles.py` — `bootstrap_after` query param plumbed to `stream_bootstrap`.
- `docs/API.md` — deletion + resume semantics.
- `tests/` — deletion-tombstone and bootstrap-resume tests.
