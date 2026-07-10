# Design — GraphRAG Delta Feed + Webhook Sync Contract

- **Date:** 2026-07-10
- **Status:** Approved (design); implementation pending
- **Scope:** Spec 1 of 2. Spec 2 (`2026-07-10-vlm-image-description-design.md`) enriches the
  same article record with VLM image descriptions and depends on this one only at the
  JSONL record schema (adds `images[].description` / `kind`).

## Purpose

A separate downstream project will build a graph RAG pipeline (Microsoft GraphRAG, likely
fine-tuned) over the documentation DocExtractor collects. That pipeline needs to:

1. **Bootstrap** its index from the full current corpus.
2. **Incrementally update** the index whenever DocExtractor's content changes — additions,
   updates, and removals — without re-ingesting everything.
3. Be **triggered automatically** ("listen") when a run produces changes, rather than polling.

This spec delivers a **pull-based delta feed** (structured JSONL, one record per article
change) plus a **webhook trigger** that nudges the downstream to pull. The webhook is the
wake-up; the feed is the reliable data channel.

## Decisions (settled during brainstorming)

- **Sync contract:** webhook triggers, API pulls. The `extraction_complete` webhook carries a
  lightweight `delta` summary; the downstream then pulls full content from the feed. Chosen
  over fat webhook payloads (unbounded size, couples content schema to delivery) and
  polling-only (under-uses the existing webhook backbone, adds latency).
- **Delta cursor:** an opaque, monotonic **watermark** backed by a `BIGSERIAL` sequence,
  covering adds/updates/removals in one ordering. Chosen over run-id (awkward for a global,
  multi-source feed) and timestamps (clock skew + out-of-commit-order writes silently skip
  records).
- **Record granularity:** one JSONL record per article change, plus tombstone records for
  removals.

## Existing primitives this builds on

- **Change detection already exists.** `Article.created_run_id` (baseline vs. incremental),
  `Article.removed_at` / `removal_run_id` (TOC drop-out), `ArticleVersion` snapshots +
  `content_hash` (content change). The pipeline already knows, per run, exactly which
  articles were added / updated / removed.
- **Webhook backbone already exists** (`app/services/webhook_dispatcher.py`,
  `app/models/webhook.py`, PR #131): HMAC signing, retry with backoff, SSRF guard, delivery
  logging, per-run subscriber planning, and an `extraction_complete` event with an extensible
  `extra` payload dict. We extend it; we do not rebuild it.
- **Streaming/bounded-memory export pattern already exists** (`app/services/exporter.py`):
  a plan pass loading lightweight columns, then keyset-batched render passes. The feed reuses
  this shape so a 4000+ page source never loads into memory at once.
- **Concurrency model:** the worker (`app/worker.py`) processes runs serially per replica, but
  `claim_next_run` uses `FOR UPDATE SKIP LOCKED`, so multiple worker replicas can run
  extractions **concurrently across sources**. The watermark design must be gap-free under
  overlapping runs (e.g. a ~1h Rubrik run overlapping a fast run).

## Architecture

### The outbox table — `content_changes`

An append-only outbox is the single source of truth for the feed. One row is appended **in the
same DB transaction** as each article add / update / removal during extraction.

| column         | type        | purpose                                                       |
|----------------|-------------|---------------------------------------------------------------|
| `id`           | `BIGSERIAL` PK | the monotonic sequence == the watermark; the only ordering key |
| `article_id`   | UUID (FK, `SET NULL`) | which article (nullable so a later article delete doesn't erase history) |
| `source_id`    | UUID (FK, `SET NULL`) | scope / sharding filter                               |
| `run_id`       | UUID (FK, `SET NULL`) | the run that produced the change; used for the safe-ceiling gate |
| `change_type`  | enum `added` \| `updated` \| `removed` | classification                        |
| `content_hash` | `String(64)` null | the article's content hash at change time (verification/dedup) |
| `topic_key`    | `String(2048)` | version-independent identity, copied so tombstones remain resolvable after the article row is gone |
| `created_at`   | timestamptz | audit only — **never** used for ordering                       |

Indexes: PK on `id` (ordering); `(run_id)` (safe-ceiling lookups); `(source_id, id)` and
`(article_id)` for filtered scans.

Retention: append-only. A future GC may trim rows older than the oldest active downstream
watermark, but out of scope here — keep all rows for now (they are small).

### Why the watermark is gap-free under concurrent runs

Ordering is by `id` alone (no timestamps). The risk with a `BIGSERIAL` cursor is the
commit-order gap: a transaction that grabbed `id=5` early may commit *after* a transaction
that grabbed `id=6`, so a reader that advanced its cursor to `6` in between would skip `5`.

The feed closes this gap with a **safe ceiling** computed per pull:

```
safe_ceiling = MIN(content_changes.id) over rows whose run is NOT in a terminal state
             = +infinity if no run is currently non-terminal
serve rows WHERE cursor < id < safe_ceiling AND run is terminal, ORDER BY id
next_since  = MAX(id served)  (or the incoming cursor if nothing was served)
```

A "terminal" run is one whose `ExtractionRun.status` is `COMPLETED` or `FAILED`. Because a
still-running run's *lowest* outbox `id` bounds every `id` it will ever write (ids only
increase), withholding everything `>=` that floor guarantees none of its future rows are
skipped once it completes. Consequences:

- **Single-worker deployments** (the common case): runs never overlap, so `safe_ceiling` is
  always `+infinity` and the feed serves everything up to the latest change.
- **Multi-replica**: a long run in flight temporarily withholds newer changes from *all*
  sources until it finishes. This is a bounded, correct delay — the downstream simply gets the
  rest on its next pull. Acceptable for an internal tool triggered per completed run.

The opaque cursor is `base64url(json({"v": 1, "seq": <id>}))`. Absent/invalid cursors are
handled explicitly (see endpoint).

### Where outbox rows are written

In `FirecrawlService.extract_source` (and the PDF path), at the existing decision points:

- **added** — where a new `Article` is created (its `created_run_id` is set). This includes the
  baseline run's articles — every new article gets an `added` row, no special-casing. The
  bootstrap snapshot (`since` omitted) is a separate, optional fast-path for a downstream
  onboarding after articles already exist; a downstream can equally start from an empty cursor
  and receive those same `added` rows through the delta path. Because records are idempotent
  upserts keyed on `id`, the two paths never cause double-ingestion.
- **updated** — where an `ArticleVersion` snapshot is taken (content hash changed).
- **removed** — where `removed_at` is stamped during TOC reconciliation. If a removed article
  later returns, that is a normal `added`/`updated` row on the returning run.

All three happen inside the run's own transaction(s), so the outbox row commits atomically
with the mutation. If the run fails/rolls back, its outbox rows roll back too.

## The endpoint — `GET /api/articles/delta`

Authenticated (same `Principal` / RBAC as `/api/articles`), RBAC-filtered to the caller's
visible vendors. Returns `application/x-ndjson` via `StreamingResponse`, keyset-batched
internally by `id` (reusing the exporter's plan/render batching) so memory stays bounded.

Query params:

| param        | meaning                                                                 |
|--------------|-------------------------------------------------------------------------|
| `since`      | opaque cursor from a prior pull. **Omitted → full bootstrap snapshot.** |
| `source_id`  | optional: restrict to one source                                        |
| `vendor_id`  | optional: restrict to one vendor (sharding)                             |

Behavior:

- **`since` present:** stream `content_changes` rows per the safe-ceiling query above, each
  joined to its live article (for content) or emitted as a tombstone (`removed`). Ordered by
  `id`.
- **`since` omitted (bootstrap):** stream every current, non-removed article the caller can see
  as a `change_type: "added"` record, and set `next_since` to the current global `MAX(id)` of
  `content_changes` (so the next delta pull continues cleanly). This is how the GraphRAG index
  does its initial build.
- **Invalid cursor:** `422` (same convention as the `/api/articles` cursor).
- **Empty delta:** a well-formed stream containing only the final control record with an
  unchanged `next_since`.

### JSONL record schema

One JSON object per line. Content record (`added` / `updated`):

```json
{
  "seq": 4811,
  "change_type": "updated",
  "id": "…article uuid…",
  "topic_key": "https://help.example.com/backup/proxies",
  "source_id": "…", "vendor": "Veeam", "product": "Backup & Replication",
  "title": "Backup Proxies",
  "source_url": "https://help.example.com/backup/proxies",
  "last_updated_at": "2026-07-01T09:12:00Z",
  "content_hash": "sha256:…",
  "estimated_tokens": 1234,
  "parent_chapter": "Deployment",
  "top_level_chapter": "Installation",
  "sort_order": 42,
  "run_id": "…",
  "content_markdown": "# Backup Proxies\n\n…",
  "images": [
    { "url": "/media/…/x.png", "alt": "topology", "description": null, "kind": null }
  ]
}
```

(`images[].description` and `kind` are populated by Spec 2; `null` until then.)

Tombstone record (`removed`):

```json
{ "seq": 4830, "change_type": "removed", "id": "…", "topic_key": "…",
  "source_id": "…", "removed_at": "2026-07-09T22:04:00Z", "run_id": "…" }
```

Final control record (always last):

```json
{ "control": "cursor", "next_since": "eyJ2IjoxLCJzZXEiOjQ4MzB9", "count": 16 }
```

Emitting the watermark as a trailing control record (rather than an HTTP header) means a
streamed client receives it reliably after consuming the whole body, and a truncated stream is
detectable (no control record → do not advance the cursor).

The provenance fields (`vendor`, `product`, `parent_chapter`, `top_level_chapter`) are derived
from the live TOC exactly as `GET /api/articles/{id}` does today, so they stay correct across
TOC rebuilds.

## Webhook contract

Extend the existing `extraction_complete` event's `extra` payload — no new event type, no new
dispatcher code. After a run finishes and its outbox rows are committed, the summary block is
computed from the run's `content_changes` rows:

```json
{
  "event": "extraction_complete",
  "timestamp": "2026-07-10T12:00:00Z",
  "run_id": "…", "source_id": "…",
  "source_name": "…", "vendor_name": "…", "product_name": "…",
  "delta": {
    "added": 12, "updated": 3, "removed": 1,
    "watermark": "eyJ2IjoxLCJzZXEiOjQ4MzB9"
  }
}
```

Downstream listener flow:

1. Receive `extraction_complete` (verify HMAC via `X-DocExtractor-Signature`).
2. Call `GET /api/articles/delta?since=<its own stored cursor>` — **not** the watermark from
   the payload. Using its own stored cursor makes a missed or failed webhook delivery
   self-healing: the next successful run's pull covers everything since the last cursor the
   downstream durably advanced.
3. Apply the JSONL records to the GraphRAG index (upsert `added`/`updated`, delete
   `removed`), then persist `next_since` from the control record.

The `delta.watermark` in the payload is informational (lets a listener log/annotate); it is not
required for correctness. The `delta` counts let a listener skip pulling when all three are 0.

## Error handling

- **Outbox write failure**: cannot happen independently — it shares the mutation's transaction.
  If the mutation commits, the outbox row commits; if it rolls back, so does the row.
- **Truncated stream** (client disconnect / server error mid-stream): no control record is
  emitted, so the client must not advance its cursor; the next pull re-serves from the last
  durable cursor. Records are idempotent upserts keyed on `id`, so re-serving is safe.
- **Deleted source / article**: `SET NULL` FKs keep outbox rows; `topic_key` is copied onto the
  row so a tombstone stays resolvable after the article row is gone.
- **Invalid/forged cursor**: `422`.
- **RBAC**: an API key that loses visibility of a vendor simply stops receiving those records;
  no error. Bootstrap and delta both apply the same `visible_vendor_ids` filter as
  `/api/articles`.

## Testing

- Outbox row written transactionally on each of add / update / remove (three cases); rolled
  back with a failed run.
- Cursor round-trip (`encode`/`decode`), invalid cursor → 422.
- **Safe-ceiling invariant**: simulate an in-flight run holding a low `id` while a terminal run
  wrote a higher `id`; assert the higher `id` is withheld until the low run is terminal, and
  that no `id` is ever skipped across two consecutive pulls.
- Bootstrap snapshot completeness: `since` omitted streams exactly the current non-removed,
  visible articles and returns the global max watermark.
- Tombstone records for removals; a removed-then-returned article yields `removed` then a later
  `added`/`updated`.
- RBAC vendor filtering on both bootstrap and delta streams.
- Webhook `delta` counts equal the run's outbox row counts by type.
- Streaming stays memory-bounded on a large source (batched keyset scan, asserted by batch
  count / peak rows loaded).

## Out of scope (this spec)

- VLM image descriptions (Spec 2) — this feed carries the fields as `null` until Spec 2 ships.
- Outbox retention/GC.
- Any change to the GraphRAG side (the downstream project consumes this contract).
