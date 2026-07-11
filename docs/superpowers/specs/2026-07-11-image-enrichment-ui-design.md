# Design — Image-Enrichment Monitoring + On-Demand Enrich

- **Date:** 2026-07-11
- **Status:** Approved (design); implementation pending
- **Builds on:** the VLM image-description feature (`2026-07-10-vlm-image-description-design.md`)
  and the delta feed (`2026-07-10-graphrag-delta-feed-design.md`).

## Purpose

The image-description enrichment phase is budget-capped (`image_vlm_max_per_run`, default 100),
so a source with many images is only partially described per run and the rest is deferred.
Today there is no way to (a) **see** which sources still have undescribed images, or (b) **run
enrichment on demand** to finish a source without a full re-scrape. This adds both: per-source +
aggregate monitoring in the UI, and an on-demand "describe missing images" action backed by a
new lightweight run kind.

## Decisions (settled during brainstorming)

- **Mechanism:** a new `kind="enrich"` queued run that skips scraping and only runs the image
  enrichment phase — reusing the run/queue/worker/Jobs infrastructure (progress, one-active-run,
  cancel), mirroring the PDF `kind="escalate"` retry.
- **Budget:** a manual enrich run **drains all** of a source's missing images (ignores the
  per-run budget). Extraction-time enrichment keeps the `image_vlm_max_per_run` cap unchanged.
- **Monitoring placement:** **both** — a per-source badge + action in `SourceList`, and an
  aggregate rollup + backlog list on the `Dashboard`.
- **Downstream (GraphRAG) safety — folded in during brainstorming:**
  1. The delta record serves `content_hash = sha256(content_markdown)` (hash of the *served*
     content) so a consumer can reliably detect enrichment updates. (Enrichment changes
     `content_markdown` but not the Article's raw-scrape `content_hash`, so the previous record
     hash would not have changed — a consumer de-duping on it would silently skip the
     descriptions.)
  2. `enrich_source_run` fires the `extraction_complete` webhook (with a `delta` summary), so an
     enrich run nudges the downstream to pull, like a normal run.

## Existing primitives this builds on

- **Enrichment phase** `app/services/image_describe.py::enrich_run_images(db, source_id, run_id)`
  — selects meaningful images, describes (cached, budgeted, circuit-broken), injects captions,
  emits `updated` `content_changes` rows; never touches `content_hash`; best-effort with rollback.
- **Run/queue infra** — `enqueue_run(db, source_id, trigger, kind)` (raises `ActiveRunExists`),
  worker dispatch on `run.kind` (`worker.py`: `escalate` vs `extract`), `retry_escalation_run`
  as the template for a non-scrape run kind (phase, `run_start` floor, completion).
- **Delta feed** — `delta_feed.py::_content_record` currently emits `"content_hash":
  article.content_hash` (line 129); the outbox `run_start` floor keeps it gap-free.
- **Webhook** — `webhook_dispatcher.prepare_run` / `run_has_subscribers` / `spawn_event` +
  `change_log.run_change_counts` (used by `extract_source`'s completion block).
- **Dashboard** — `app/routes/dashboard.py` (`GET /api/dashboard/sources`), consumed by
  `Dashboard.tsx`; `SourceList.tsx` lists a product's sources; `JobsView.tsx` shows runs (with
  `kind`).

## Backend

### 1. `enrich_run_images` — optional unlimited budget

Add a keyword param so the manual enrich run can drain everything:

```python
async def enrich_run_images(db, source_id, run_id, *, describe=describe_image,
                            max_new: int | None = None) -> int:
    ...
    budget = settings.image_vlm_max_per_run if max_new is None else max_new
    ...
    return described   # number of images newly described this run
```

- Extraction-time call is unchanged (`max_new` omitted → budgeted).
- Returns the count of images newly described (the extraction caller ignores it; the enrich run
  uses it for run counters).

### 2. Trigger endpoint — `POST /api/extraction/enrich/{source_id}`

In `app/routes/extraction.py`, alongside `trigger_extraction`:

- `authorize_source(write)`; 404 if the source is missing.
- 409 `"Image descriptions are not enabled"` if `not settings.image_vlm_enabled`.
- Compute the source's **pending** count (candidate images: `description IS NULL AND
  is_meaningful IS NOT FALSE`). 409 `"No images need description for this source"` if 0.
- `enqueue_run(db, source_id, trigger="manual", kind="enrich")` → 409 on `ActiveRunExists`.
- Return `ExtractionTriggerResponse(status="pending", …)`.

### 3. Worker dispatch + `enrich_source_run`

`worker.py`, in the kind branch:

```python
if run_kind == "escalate":
    await firecrawl_service.retry_escalation_run(db, source_id, run_id)
elif run_kind == "enrich":
    await firecrawl_service.enrich_source_run(db, source_id, run_id)
else:
    await firecrawl_service.extract_source(db, source_id, run_id=run_id)
```

`FirecrawlService.enrich_source_run(db, source_id, run_id)` — mirrors `retry_escalation_run`:

1. Load source + run; set `run.current_phase = "image_enrich"`, `source.status = EXTRACTING`;
   `await webhook_dispatcher.prepare_run(db, run_id, source_id)`.
2. Commit a **`run_start` floor**: `await change_log.record_run_start(db, source_id=source_id,
   run_id=run_id); await db.commit()` (keeps the delta feed gap-free while the run is active).
3. `described = await image_describe.enrich_run_images(db, source_id, run_id, max_new=_UNLIMITED)`
   where `_UNLIMITED = 10**9` (drain all).
4. Reload the run; set `articles_updated = described`, `articles_extracted = described`,
   `status = COMPLETED`, `completed_at`, `source.status = COMPLETED`, `source.last_extracted_at`;
   `await db.flush()`.
5. **Fire `extraction_complete`** (gated on `run_has_subscribers(run_id, "extraction_complete")`)
   with the same `delta` summary block `extract_source` uses (from
   `change_log.run_change_counts(db, run_id)` + `encode_delta_cursor(max content_changes.id)`),
   then `webhook_dispatcher.finish_run(run_id)`.

Best-effort semantics come from `enrich_run_images` (it rolls back on failure); the circuit
breaker still applies, so on a VLM outage the run completes with some images still pending — the
UI shows the remaining count and the user can re-run.

### 4. Delta record serves a served-content hash

In `delta_feed.py::_content_record`, replace:

```python
"content_hash": article.content_hash,
```
with the hash of the *actual served content*:

```python
"content_hash": hashlib.sha256(article.content_markdown.encode("utf-8")).hexdigest(),
```

(add `import hashlib`). This makes the delta record's `content_hash` change exactly when the
served `content_markdown` changes — including caption injection — so a downstream consumer can
safely de-dup / change-detect on it. It does **not** affect extraction's internal change
detection, which uses the Article's raw-scrape `content_hash` separately. Bootstrap records get
the same treatment (they build content records via the same helper).

### 5. Enrichment stats — `GET /api/dashboard/enrichment`

In `app/routes/dashboard.py`, RBAC-filtered by `principal.visible_vendor_ids()`:

- One aggregation over `article_images` joined `articles → documentation_sources → products →
  vendors`, grouped by source:
  - **`described`** = `count(*) FILTER (WHERE description IS NOT NULL)`
  - **`pending`** = `count(*) FILTER (WHERE description IS NULL AND is_meaningful IS NOT FALSE)`
    (unevaluated *or* meaningful-undescribed; decorative `is_meaningful = false` excluded).
- **`active_run`** per source: `source_id IN (SELECT source_id FROM extraction_runs WHERE status
  IN ('PENDING','RUNNING','PAUSED'))`.
- Aggregate: `sum(described)`, `sum(pending)`, `count(sources WHERE pending > 0)`.

Response (`DashboardEnrichmentResponse`):

```json
{
  "aggregate": { "described": 12400, "pending": 64431, "sources_with_backlog": 240 },
  "sources": [
    { "source_id": "…", "vendor": "Veeam", "product": "Backup & Replication",
      "name": "User Guide", "described": 120, "pending": 180, "active_run": false }
  ]
}
```

A source is fully enriched when `pending == 0`.

## Frontend

### 6. API client + types

`src/types/index.ts`: `SourceEnrichment { source_id, vendor, product, name, described, pending,
active_run }` and `EnrichmentSummary { aggregate: { described, pending, sources_with_backlog },
sources: SourceEnrichment[] }`.

`src/api/client.ts`: `getEnrichmentStats(): Promise<EnrichmentSummary>` (`GET
/api/dashboard/enrichment`); `enrichSource(sourceId): Promise<...>` (`POST
/api/extraction/enrich/{sourceId}`).

### 7. `SourceList` — per-source badge + action

- On mount, fetch `getEnrichmentStats()` and index by `source_id`.
- Each source row shows a badge when it has images: `🖼 {described}/{described+pending} described`
  and, when `pending > 0`, `· {pending} pending`.
- A **"Describe missing images"** button, shown when `pending > 0`, disabled when `active_run`
  (title: "a run is already active"). On click → `enrichSource(id)` → toast ("Enrichment queued")
  → refresh stats. A `409` surfaces its `detail` as the toast.

### 8. `Dashboard` — Image-enrichment section

- A new card: corpus rollup `{described} / {described+pending} images described` +
  `{sources_with_backlog} sources with a backlog`.
- A backlog list (sources with `pending > 0`, sorted by `pending` desc) — each row: vendor /
  product / name, `{pending} pending`, and an **Enrich** button (same call + gating as SourceList).

### 9. `JobsView` — label enrich runs

Runs already carry `kind`. Render a small "Enrich" badge for `kind === "enrich"` (alongside the
existing extract/escalate rendering) so enrich runs are distinguishable in the run list.

## Interaction with the delta feed / GraphRAG downstream

- Enrich runs emit `updated` delta records whose `content_markdown` now carries captions and
  whose `images[].description`/`kind` are populated; with change #4 the record's `content_hash`
  reflects that served content, so a consumer can de-dup/change-detect on it reliably.
- Enrich runs fire `extraction_complete` (change #3) so the consumer is nudged to pull.
- **One-time wave:** enriching the corpus produces one `updated` record per enriched article —
  the consumer re-ingests them through normal incremental sync.
- **Head-of-line (documented, not solved):** a drain-all enrich run is long, and while `RUNNING`
  it pins the global delta-feed `_safe_ceiling`, so other sources' delta rows are withheld
  (gap-free, just delayed) until it completes. Pre-existing; acceptable for the internal tool.

## Error handling

- Trigger: 409 for feature-disabled / nothing-to-enrich / already-active; 404 for missing source;
  403 via `authorize_source` for RBAC.
- `enrich_source_run`: `enrich_run_images` is best-effort (rolls back on failure); the run still
  completes so the source doesn't get stuck `EXTRACTING`. A circuit-breaker stop leaves images
  pending → visible in the UI → re-runnable.
- Stats endpoint: empty/zero when a source has no images; RBAC-empty returns an empty list.

## Testing

- `enrich_run_images(max_new=…)`: unlimited drains all (describes more than the default budget in
  one call); default still caps at `image_vlm_max_per_run`.
- Trigger endpoint: queues a `kind="enrich"` run; 409 nothing-to-enrich (all described / no
  images); 409 already-active; 409 feature-disabled.
- Worker dispatch → `enrich_source_run`: writes a `run_start` floor, drains all missing, sets
  counters, completes; leaves `content_hash` on the Article untouched; fires `extraction_complete`
  with a `delta` summary when a subscriber exists.
- Delta record: `content_hash` equals `sha256(content_markdown)` and changes after caption
  injection (an enrich `updated` record's hash differs from the pre-enrich record's).
- Stats endpoint: described/pending counts (decorative excluded, unevaluated counted as pending),
  `active_run` flag, aggregate rollup, RBAC scoping.
- Frontend: badge renders from stats; button gated on `pending>0` and `!active_run`; click calls
  the endpoint and refreshes; Dashboard rollup + backlog render.

## Out of scope

- Auto-repeat/loop to drain the whole corpus (the on-demand action + scheduled runs cover it).
- Re-describing cached images after a model upgrade (backfill), and PDF-figure descriptions —
  both already out of scope for the base feature.
- Solving the global-ceiling head-of-line amplification (tracked as a delta-feed follow-up).
