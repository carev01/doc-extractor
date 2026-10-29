# DocExtractor — API Reference

All endpoints are served at `/api/*`. Interactive Swagger UI is available at `/docs`
when the backend is running.

## Authentication

Authentication is opt-in (disabled when `DOCEXTRACTOR_AUTH_JWT_SECRET` is empty).

When enabled, every `/api/` request must include either:
- `X-API-Key: <key>` — API key authentication
- `Authorization: Bearer <jwt>` — JWT authentication

### RBAC

| Method | Minimum Role |
|--------|-------------|
| GET, HEAD | `read_only` |
| POST, PUT, PATCH, DELETE | `read_write` |

Admin-only endpoints (user management, jobs, auth realms) require `admin`.

---

## Vendors

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/vendors` | List vendors (paginated, `offset` + `limit`) |
| `POST` | `/api/vendors` | Create a vendor (`name`, `website`) |
| `GET` | `/api/vendors/{id}` | Get a vendor |
| `PATCH` | `/api/vendors/{id}` | Update a vendor |
| `DELETE` | `/api/vendors/{id}` | Delete a vendor |

---

## Products

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/products` | List products (optional `vendor_id` filter) |
| `POST` | `/api/products` | Create a product under a vendor |
| `GET` | `/api/products/{id}` | Get a product |
| `PATCH` | `/api/products/{id}` | Update a product |
| `DELETE` | `/api/products/{id}` | Delete a product |
| `POST` | `/api/products/{id}/versions/enable` | Enable versioning for a product |
| `POST` | `/api/products/{id}/versions/preview` | Dry-run a bump: resolve each templated source's new URL |
| `POST` | `/api/products/{id}/versions/bump` | Bump product version |

### URL templates

A versioned source stores a `url_template` whose placeholders are substituted to
produce the live `base_url`:

| Placeholder | Supplied by | Notes |
|---|---|---|
| `{version}` | the operator, at bump time | Appears in the URL verbatim. |
| `{rev}` | the vendor, resolved automatically | An opaque per-release token (e.g. a build id) that is not derivable from the version. Re-resolved before every run and never stored resolved. |

`{rev}` exists because some portals change more than the version segment between
releases. Cohesity's NetBackup docs address a page as
`/docs/<product>/<version>/<publication>-<build>-0/<topic>-<build>`: the
publication and topic ids are stable, but `<build>` is re-minted per release and
occurs twice. Templating only the version would leave the previous release's
build id in both the URL (which the vendor 404s) and the article's `topic_key`,
so every article would look new at each release.

`POST /versions/preview` reports, per templated source, a `status` of `ok`
(nothing to resolve), `resolved` (the vendor's current token was read), or
`unresolved` (it could not be — `resolved_url` then carries the *current*
release's token so it stays well-formed). `POST /versions/bump` refuses with
`409` when any source is `unresolved`, before it overwrites `previous_version`;
pass `force: true` to proceed and let the next run resolve it.

---

## Sources

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/sources` | List sources (optional `product_id` filter) |
| `POST` | `/api/sources` | Create a documentation source (web) |
| `POST` | `/api/sources/pdf` | Create a PDF source (upload PDF) |
| `PUT` | `/api/sources/{id}/pdf` | Replace PDF for an existing source |
| `GET` | `/api/sources/{id}` | Get a source |
| `PATCH` | `/api/sources/{id}` | Update a source |
| `DELETE` | `/api/sources/{id}` | Delete a source |
| `POST` | `/api/sources/import` | Bulk import sources |
| `POST` | `/api/sources/{id}/detect-version-token` | Auto-detect version token from page |
| `GET` | `/api/sources/{id}/changelog` | Get changelog (versioned article diffs) |
| `GET` | `/api/sources/{id}/browse` | Browse articles by TOC structure |

---

## Extraction

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/extraction/trigger/{source_id}` | Trigger an extraction run (`?force=true` re-converts a PDF whose bytes are unchanged) |
| `GET` | `/api/extraction/bulk/preview` | Sources a bulk extract would cover — exactly one of `?product_id=` / `?vendor_id=` — grouped by product, each with `eligible` and a skip `reason` (see [Bulk extract](#bulk-extract)) |
| `POST` | `/api/extraction/bulk` | Queue an ordinary extraction for each eligible source in `{"source_ids": [...]}`; returns `queued` / `skipped` |
| `POST` | `/api/extraction/enrich/{source_id}` | Queue an image-enrichment-only run (`kind=enrich`; describe all missing images, no re-scrape). 409 if descriptions are disabled, nothing needs describing, or a run is already active. |
| `GET` | `/api/extraction/runs` | List runs (optional `source_id` filter) |
| `GET` | `/api/extraction/runs/{run_id}` | Get run status |
| `GET` | `/api/extraction/runs/{run_id}/logs` | Get run logs |
| `POST` | `/api/extraction/runs/{run_id}/cancel` | Cancel a running extraction |
| `POST` | `/api/extraction/runs/{run_id}/pause` | Pause a running extraction |
| `POST` | `/api/extraction/runs/{run_id}/resume` | Resume a paused extraction |
| `POST` | `/api/extraction/runs/{run_id}/retry-escalation` | Retry VLM escalation for failed pages |
| `POST` | `/api/extraction/runs/{run_id}/retry-blocked` | Re-scrape only the pages a run recorded as bot-blocked (no TOC re-discovery). 409 if nothing blocked or a run is active. |
| `POST` | `/api/extraction/resanitize/{source_id}` | Re-sanitize all articles for a source |

### Bulk extract

The product view's **Extract sources** and the vendor view's **Extract products** queue one ordinary `extract` run (`trigger: "bulk"`) per chosen source. They go through the normal queue, so they still run **one at a time**, oldest first, and progress shows on each source as usual. `force` and `allow_toc_collapse` are deliberately not offered in bulk.

A source is skipped, with a `reason`, wherever a single trigger would refuse it. Eligibility is re-checked when you `POST`, because a run may have started since the preview.

| `reason` | Meaning |
|---|---|
| `active_run` | A run is already queued, running or paused for this source (never a second one) |
| `session_expired` | The source's login realm has expired; log in or re-upload cookies first |
| `no_write_access` | The caller has only read access to the source's vendor |
| `not_found` | No such source, or one in a vendor the caller cannot see (not revealed) |

```bash
curl -s "$API/api/extraction/bulk/preview?product_id=$PRODUCT" -H "X-API-Key: $KEY"
curl -s -X POST "$API/api/extraction/bulk" -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' -d '{"source_ids": ["…", "…"]}'
# → {"queued": [{"source_id": "…", "run_id": "…"}], "skipped": [{"source_id": "…", "reason": "active_run"}]}
```

---

## Articles

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/articles` | Search articles (`source_id`, `q`, `offset`, `limit`) |
| `GET` | `/api/articles/{id}` | Get article detail (with content + images) |
| `GET` | `/api/articles/delta` | Streaming JSONL delta feed for programmatic sync (see [Delta Feed](#delta-feed)) |
| `GET` | `/api/articles/toc/{source_id}` | Get TOC tree for a source |
| `GET` | `/api/articles/{id}/versions` | List article versions |
| `GET` | `/api/articles/{id}/versions/{version_id}` | Get a specific version |
| `GET` | `/api/articles/{id}/versions/{version_id}/diff` | Diff a version against the next or current version (`against=next\|current`) |

### Article timestamps

`GET /api/articles/{id}` and every delta-feed content record carry five timestamps. Only two of them describe the **content**; the others describe our crawling.

| Field | Meaning |
|-------|---------|
| `content_changed_at` | The served markdown became current. **Order by this.** |
| `content_changed_basis` | How that value was derived — `exact`, `lower_bound`, or `first_seen`. |
| `source_changed_at` | The raw scrape changed — i.e. the vendor's own page changed, as opposed to us re-rendering or enriching it. |
| `last_updated_at` | A revision date the vendor declares on the page. Null for most of the corpus. |
| `last_updated_source` | Where that vendor date came from — `vendor_meta` or `page_markup`. Non-null exactly when `last_updated_at` is. |
| `created_at` / `extracted_at` | When **we** first captured and last crawled the page. Not content-change signals — a bulk run stamps thousands of articles within the same minute. |

Full semantics, including the `changed no later than T` caveat and the provenance tiers, are under [Delta Feed → Timestamps](#timestamps--which-one-to-order-by).

### Version history

`GET /api/articles/{id}/versions` returns an article's prior snapshots, newest first — enough to reconstruct its revision timeline without storing one yourself.

```json
{
  "article_id": "article-uuid",
  "current_hash": "sha256 of the live content",
  "total": 3,
  "versions": [
    {
      "id": "version-uuid",
      "content_hash": "sha256 of THIS snapshot",
      "extracted_at": "2026-08-01T14:03:11Z",
      "extraction_run_id": "run-uuid",
      "has_diff": true,
      "content_size_bytes": 18432,
      "version": "11.46",
      "source_url": "https://help.example.com/old/path"
    }
  ]
}
```

Each row holds a **superseded** snapshot; the live content is on the article itself (`current_hash`). `extracted_at` is therefore the moment that snapshot **was replaced** — so the newest row's timestamp is when the current content became current. A row exists only when the *raw scrape* changed, which is what makes `source_changed_at` a vendor-change signal rather than a re-render one.

`source_url` is the URL the snapshot was captured at, preserved across relocations and version bumps. Add `?skip=&limit=` to page (max 200).

---

## Delta Feed

`GET /api/articles/delta` streams article changes as **JSON Lines** (`application/x-ndjson`) for a downstream consumer (e.g. a graph-RAG indexer) to bootstrap from and then keep in sync. It is the reliable, pull-based data channel; the `extraction_complete` [webhook](#webhooks) is the "wake up and pull" nudge.

**Query parameters**

| Param | Description |
|-------|-------------|
| `since` | Opaque cursor from a prior pull's control record. **Omit for a full bootstrap snapshot.** |
| `source_id` | Optional — restrict to one source |
| `vendor_id` | Optional — restrict to one vendor (sharding) |
| `bootstrap_after` | Optional, **bootstrap only** — resume a dropped snapshot after this article id (ignored when `since` is set). See [Resuming a dropped bootstrap](#resuming-a-dropped-bootstrap). |

- **`since` omitted → bootstrap**: the **first** line is a control record `{"control":"bootstrap_start","next_since":"…"}` carrying the watermark up front; then every current, visible, non-removed article streams as `change_type: "added"` (with `seq: null`); the terminal control record repeats the same `next_since`.
- **`since` present → incremental**: streams `added`/`updated` content records and `removed` tombstones since that watermark.

### Timestamps — which one to order by

`created_at` and `extracted_at` describe when **we crawled**, not when the vendor changed anything. A bulk run stamps thousands of articles within the same minute (2,470 in one minute, measured), so ordering facts by them is effectively ordering by crawler visit order. Use these instead:

| Field | Moves when | Use it for |
|-------|-----------|------------|
| `content_changed_at` | the **served markdown** changed — a re-scrape with different bytes, **or** an image-caption enrichment | Ordering records in time. This is the one to inherit as a fact's timestamp. |
| `source_changed_at` | the **raw scrape** changed — i.e. the vendor's own page changed | Telling "they rewrote the page" from "we re-rendered or enriched it". |
| `last_updated_at` | the vendor declares a revision date on the page | Preferred over both when non-null — but it is null for most of the corpus. |

**`last_updated_source`** says where a vendor date came from, and is non-null exactly when `last_updated_at` is:

| Value | Meaning |
|-------|---------|
| `vendor_meta` | A revision date the vendor published as metadata — a `<meta>` tag (`article:modified_time`, `og:updated_time`, `dcterms.modified`, `ms.date`, `datemodified`) or schema.org `dateModified` in a JSON-LD block. |
| `page_markup` | A `<time datetime>` element in the article body — structured markup the vendor emitted, not a date read out of prose. |

Two signals are deliberately **not** implemented, because measurement showed they would fabricate dates:

- **HTTP `Last-Modified`** returns the *current day* for AWS docs (CDN cache behaviour) and disagreed with Microsoft's own page date by four months. It would mark every page as changed daily.
- **Sitemap `lastmod`** is not fetched — it is unknown whether vendors emit a revision date or a generation date there.

A `page_text` tier (a date scraped out of visible prose) is reserved and unused. If it ever appears, treat it with more suspicion than the two above.

Note that `vendor_meta` values are sometimes **day-granular** — `ms.date` is always midnight, for instance — so they cannot order two changes within the same day. If you prefer `last_updated_at` over `content_changed_at` when both are present, you are trading precision for provenance; `content_changed_at` carries microsecond resolution.

A re-crawl returning identical bytes moves `extracted_at` and **neither** of the first two. That is the point of them.

`content_changed_at` is deliberately anchored to the *served* markdown — the same bytes the `content_hash` above is taken over — so a consumer that re-ingests on a hash change can always date that change. Anchoring it to the raw scrape would leave a class of articles (22.7% of the corpus, where caption injection or image-URL rewriting has modified the served text) re-ingesting changed bytes under an unchanged timestamp.

**`content_changed_basis`** says how the value was arrived at, because history predating the change outbox cannot be dated exactly:

| Value | Meaning |
|-------|---------|
| `exact` | From the change outbox — the real instant the served markdown became current. All new changes are `exact`. |
| `lower_bound` | Pre-outbox. The last **raw** change, from archived versions. May be **early**: an enrichment before the outbox shipped changed the served text without archiving a version, so it is undated. |
| `first_seen` | Never observed changing; its first-seen date. Still stable across re-crawls. |

Treat `content_changed_at` as **"changed no later than T"** in all cases: it is bounded below by crawl frequency, so a page that changed in March and was first re-crawled in August reports August. For `lower_bound` it is weaker still — prefer restricting destructive operations (expiring or invalidating a fact) to `exact` values.

The response is RBAC-filtered to the caller's visible vendors. The **last line is always a control record** — `{"control":"cursor","next_since":"…","count":N}` (for a bootstrap, the same `next_since` as the leading `bootstrap_start` line). A truncated stream lacks it, so clients must only advance their stored cursor on a clean finish. Ordering is gap-free even when extraction runs overlap (see [Architecture → Delta Feed](ARCHITECTURE.md#delta-feed)).

**Content record** (`added` / `updated`):

```json
{
  "seq": 4811,
  "change_type": "updated",
  "id": "article-uuid",
  "topic_key": "https://help.example.com/backup/proxies",
  "source_id": "source-uuid",
  "vendor": "Veeam",
  "product": "Backup & Replication",
  "title": "Backup Proxies",
  "source_url": "https://help.example.com/backup/proxies",
  "last_updated_at": "2026-07-01T09:12:00Z",
  "last_updated_source": "vendor_meta",
  "content_changed_at": "2026-08-01T14:03:11Z",
  "content_changed_basis": "exact",
  "source_changed_at": "2026-05-05T08:30:00Z",
  "content_hash": "…",
  "estimated_tokens": 1234,
  "parent_chapter": "Deployment",
  "top_level_chapter": "Installation",
  "sort_order": 42,
  "run_id": "run-uuid",
  "content_markdown": "# Backup Proxies\n\n…",
  "images": [{ "url": "/media/…/x.png", "alt": "topology", "description": null, "kind": null }]
}
```

**Tombstone record** (`removed`):

```json
{ "seq": 4830, "change_type": "removed", "id": "article-uuid", "topic_key": "…",
  "source_id": "source-uuid", "removed_at": "2026-07-09T22:04:00Z", "run_id": "run-uuid" }
```

**Deletions propagate.** Removing a source, product, or vendor emits a `removed` tombstone for every one of its live articles — carrying the original article `id` (preserved through the hard delete) — so a consumer that processes `removed` records drops those nodes instead of orphaning them. These out-of-band deletions are not part of an extraction run, so their tombstones have `"run_id": null`. They are served with the same gap-free ordering as run-driven removals.

`images[].description` / `kind` (also on `GET /api/articles/{id}`) carry the VLM-generated image description and its classification (`screenshot`/`diagram`/`chart`/`photo`/`other`) when image descriptions are enabled (`DOCEXTRACTOR_IMAGE_VLM_ENABLED`); they are `null` for images that weren't described (feature off, decorative image, or not yet processed).

Each record's `content_hash` is the **SHA-256 of the served `content_markdown`** — so it changes whenever the served content changes (including when image captions/descriptions are injected). Consumers can safely de-dup / change-detect on it. (It is distinct from the internal raw-scrape fingerprint the pipeline uses for change detection.)

**Typical consumer loop**

1. Bootstrap: `GET /api/articles/delta` → apply every record, store `next_since`.
2. Subscribe a webhook to `extraction_complete`.
3. On each notification, `GET /api/articles/delta?since=<stored cursor>` → upsert `added`/`updated`, delete `removed`, then persist the new `next_since`. Use your own stored cursor (not the one in the webhook), so a missed delivery self-heals on the next pull.

### Connecting a downstream after the corpus already exists

You do **not** need to re-extract anything to onboard a consumer that connects
late (i.e. after sources have already been pulled). The bootstrap snapshot is a
**direct scan of the live article table**, not a replay of the change outbox —
so it returns the entire current corpus regardless of when each article was
extracted or whether the consumer existed at extraction time. Connect it now,
take one full snapshot, then live off incremental deltas:

```bash
# 1. Full bootstrap — every current, visible, non-removed article as `added`.
#    Stream it to your indexer and capture next_since from the final control line.
curl -sN -H "X-API-Key: $KEY" \
  https://docextractor.example/api/articles/delta > snapshot.ndjson
CURSOR=$(tail -n1 snapshot.ndjson | jq -r 'select(.control=="cursor").next_since')

# 2. Thereafter, pull only what changed since your stored cursor (repeat on the
#    extraction_complete webhook, or poll).
curl -sN -H "X-API-Key: $KEY" \
  "https://docextractor.example/api/articles/delta?since=$CURSOR"
```

Two guarantees make this safe:

- **No gap between snapshot and first delta.** The bootstrap's `next_since` is
  pinned to the current outbox watermark but never past the safe ceiling of any
  still-running extraction — so a change committed *while you were streaming the
  snapshot* is re-served by your first incremental pull rather than skipped.
- **Enrichment updates are visible.** Because each record's `content_hash` is
  the SHA-256 of the served markdown, a later image-description run surfaces the
  affected pages as `updated` deltas with a changed hash — re-index just those.

To shard or backfill a subset, add `?source_id=…` or `?vendor_id=…` to either
call; both are RBAC-scoped to the API key's visible vendors.

### Resuming a dropped bootstrap

For a large corpus the bootstrap snapshot can be many thousands of records; if
the connection drops mid-stream you can resume instead of restarting, using
`bootstrap_after` plus the up-front `bootstrap_start` watermark:

1. On the **first** attempt, read `next_since` from the leading
   `{"control":"bootstrap_start",…}` line and store it immediately. Apply the
   `added` records, tracking the highest article `id` you have applied.
2. If the stream ends **without** the terminal `{"control":"cursor",…}` line,
   resume: `GET /api/articles/delta?bootstrap_after=<highest id applied>`.
   **Keep your originally-stored `next_since`** — ignore the resumed stream's
   own `bootstrap_start` value (it is recomputed at resume time).
3. Repeat until you receive the terminal `cursor` line. Only then begin
   incremental pulls with `?since=<stored next_since>`.

Anchoring incremental to the **first** attempt's watermark is what keeps resume
correct: an update to an already-emitted (lower-id) article that lands between
your original start and the resume falls *below* that watermark's successors and
is therefore replayed by your first incremental pull (idempotent upserts absorb
any overlap). A watermark recomputed at resume time would skip it.

```bash
# resume after the last id you successfully applied, keeping your stored $CURSOR
curl -sN -H "X-API-Key: $KEY" \
  "https://docextractor.example/api/articles/delta?bootstrap_after=$LAST_ID"
```

---

## Export

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/export` | Create an export job |
| `GET` | `/api/export/jobs` | List export jobs |
| `GET` | `/api/export/jobs/{job_id}` | Get export job status |
| `POST` | `/api/export/jobs/{job_id}/cancel` | Cancel a pending export |
| `GET` | `/api/export/download/{export_id}` | Download an export (zip) |
| `GET` | `/api/export/download/{export_id}/{filename}` | Download a single file |
| `GET` | `/api/export/list` | List completed exports |
| `DELETE` | `/api/export/{export_id}` | Delete an export |
| `GET` | `/api/export/product/{id}/preview` | Project a product export's size |
| `POST` | `/api/export/product/{id}` | Export every source of a product |
| `GET` | `/api/export/vendor/{id}/preview` | Per-product size breakdown of a vendor export |
| `POST` | `/api/export/vendor/{id}` | Export the sources of a vendor's products (optional `product_ids`) |
| `GET` | `/api/export/batches/{batch_id}` | Product/vendor export status, per source |
| `GET` | `/api/export/batches/{batch_id}/download` | Combined zip, a folder per source (per product, then source, for a vendor export) |

### Export Request Body

```json
{
  "source_id": "uuid",
  "format": "markdown",
  "article_ids": ["uuid1", "uuid2"],
  "split_by": "tokens",
  "max_tokens_per_file": 500000,
  "respect_chapters": false
}
```

- `article_ids` — optional; omit for full export
- `split_by` — `"articles"`, `"size"` or `"tokens"`; omit for a single file. The
  matching limit is `max_articles_per_file`, `max_file_size_bytes` or
  `max_tokens_per_file` (a plain token count); an omitted limit falls back to
  the server default (50 articles / 10 MB / 500,000 tokens). An article is never
  split across files.
- `format` — `"markdown"` or `"pdf"`
- `include_images` — **defaults to `false`**. Image payloads dwarf the text
  (one product carries 27 MB of markdown against 495 MB of images), so images
  are opt-in. Ignored for `pdf`, which embeds them.

### Product-level export

`POST /api/export/product/{id}` exports every source of a product. It fans out
into one ordinary per-source export job each, sharing a `batch_id` — so a source
that fails costs only itself, each keeps the normal retry/cancel behaviour, and
the worker can interleave extraction runs between sources.

The body is a **subset** of the single-source request: `split_by` and its limits,
`respect_chapters`, `format`, `include_images`, an optional `topic_query`, and an
optional `source_ids` to restrict the batch. `article_ids` and `toc_entry_ids`
identify rows within one source, so they have no meaning here and are not
accepted. Sources holding no articles are skipped and named in `skipped`, since
exporting one would only ever produce a failure.

```json
{ "batch_id": "uuid", "total": 21, "skipped": ["Release Notes"] }
```

Poll `GET /api/export/batches/{batch_id}` for per-source status; `finished` turns
true once nothing is pending or running. `GET /api/export/batches/{batch_id}/download`
returns one zip with a folder per source, built from whatever has completed —
available before the whole batch finishes, and `409` until at least one source has.

The combined zip is a **cache**: it is rebuilt on demand and retention treats it
accordingly, dropping it by age, dropping it once its constituent jobs are gone,
and spending it before evicting any real export under the size cap.

---

## Tiers

Admin-only. A tier is an operator-defined label on a vendor (e.g. "Tier 1"); a job can target a tier and picks up whichever vendors carry it when the job fires.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/tiers` | List tiers (`id`, `name`, `sort_order`, `vendor_count`, `job_count`) |
| `POST` | `/api/tiers` | Create a tier: `{name, sort_order?}` (`sort_order` defaults to last). `409` on a duplicate name |
| `PATCH` | `/api/tiers/{id}` | Rename / reorder: `{name?, sort_order?}` |
| `DELETE` | `/api/tiers/{id}` | `204`; `409 {detail, vendors, jobs}` while any vendor or job still references it |
| `PUT` | `/api/tiers/{id}/vendors` | Bulk-tier: `{vendor_ids}` moves those vendors into the tier (additive; other vendors unchanged). Returns `{tier_id, updated}` |
| `PUT` | `/api/tiers/none/vendors` | Same body; removes the given vendors from whatever tier they are in |

An unknown tier or vendor id is a `404` naming the ids, and nothing is applied. `VendorResponse` carries `tier_id`.

```bash
curl -X PUT $API/api/tiers/$TIER_ID/vendors -H 'Content-Type: application/json' \
  -d '{"vendor_ids": ["<vendor-uuid>", "<vendor-uuid>"]}'
# -> {"tier_id": "…", "updated": 2}
```

---

## Jobs (Scheduled Extraction)

Admin-only. A job is a schedule plus a **coverage**: at most one tier, plus explicit vendor / product / source targets. Coverage is resolved to concrete sources each time the job fires, so new products or re-tiered vendors are picked up without editing the job. A job with `enabled=false` or no `schedule_kind` is a manual-only group.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/jobs` | List jobs (`{jobs, total}`) |
| `POST` | `/api/jobs` | Create a job (`name` + schedule fields + `tier_id?`) |
| `GET` | `/api/jobs/{id}` | Get a job |
| `PATCH` | `/api/jobs/{id}` | Partial update; omitted fields are unchanged |
| `DELETE` | `/api/jobs/{id}` | Delete a job |
| `PUT` | `/api/jobs/{id}/coverage` | Replace the explicit targets: `{source_ids}` |
| `POST` | `/api/jobs/schedule/preview` | Next three fire times for a schedule, nothing saved |
| `GET` | `/api/jobs/catalog` | Vendor > product > source tree for the coverage picker |
| `GET` | `/api/jobs/schedule` | Per-product last successful refresh and next scheduled one |
| `POST` | `/api/jobs/{id}/run` | Fire now (`trigger="manual"`); `409` if the job covers no sources |
| `GET` | `/api/jobs/{id}/runs` | Runs of one job (`limit`, default 50) |
| `GET` | `/api/jobs/runs` | Recent runs across all jobs, newest first (`limit`, default 30) |

The per-source assign endpoints (`/api/jobs/{id}/sources[/{source_id}]`) and the old frequency fields are gone.

### Schedule fields

Schedules are friendly fields; `cron` is **derived** and read-only, as is `next_run_at` (null while disabled).

| Field | Meaning |
|-------|---------|
| `schedule_kind` | `daily`, `weekly`, `monthly_day` or `monthly_nth` |
| `time_of_day` | `HH:MM`, default `02:00` |
| `timezone` | IANA name, default `UTC`; unknown names are `422` |
| `weekday` | 0-6, **0 = Sunday** (`weekly`, `monthly_nth`) |
| `interval_months` | One of 1, 2, 3, 4, 6, 12 (it must divide a year); `monthly_*` only, default 1 |
| `start_month` | 1-12, the month the cycle is anchored to; defaults to next month in the job's timezone |
| `day_of_month` | 1-28 (`monthly_day`) |
| `nth` | `first` or `last` weekday of the month (`monthly_nth`) |
| `enabled` | An enabled job must have a `schedule_kind` (`422` otherwise) |
| `tier_id` | Optional tier to cover; unknown tier is `404` |

Fields a kind does not use are cleared. Validation errors are `422 {"detail": message}`.

`JobResponse` adds `last_run_at`, `targets {vendor_ids, product_ids, source_ids}`, `coverage {sources, products, overlap_sources, login_sources}` (sources behind an auth realm; overlap = sources another job also covers) and `next_runs` (next three fire times; empty when disabled).

`PUT /coverage` takes the picked sources and stores their normalized form: a fully ticked product becomes a product target and a fully ticked vendor a vendor target, so later additions are included automatically. Unknown ids are `404` and nothing changes. It returns the `JobResponse`.

```bash
# Every 2nd month, first Sunday, 03:00 Europe/Lisbon, starting in November
curl -X POST $API/api/jobs/schedule/preview -H 'Content-Type: application/json' \
  -d '{"kind":"monthly_nth","nth":"first","weekday":0,"interval_months":2,"start_month":11,"time_of_day":"03:00","timezone":"Europe/Lisbon"}'
# -> {"next_runs": ["…", "…", "…"]}
```

### Catalog

`GET /api/jobs/catalog` returns `[{vendor_id, vendor_name, tier_id, products: [{product_id, product_name, sources: [{source_id, source_name, auth_realm}]}]}]`; `auth_realm` is the realm name or null.

### Schedule view

`GET /api/jobs/schedule` returns `{products: [...]}`, one row per product: `product_id`, `product_name`, `vendor_name`, `tier_name`, `jobs` (`[{name, enabled}]`, so a disabled job can be marked), `source_count`, `last_ok_at`, `latest`, `next_at`, `scheduled_sources`, and per-source `sources`. `latest` is `{at, outcome, trigger, job_name, updated, reason, blocked}`; a failed run's `reason` is its error message (truncated) and its `updated` is 0, and a skip recorded by a job run competes with runs by time. A product's `last_ok_at` is the oldest across its sources (null if any never succeeded); `next_at` is the latest across scheduled sources and comes only from enabled jobs. A refresh counts only if it is a completed `extract` run with no `blocked_pending`.

### Job runs

`JobRunResponse`: `id`, `job_id`, `job_name`, `status`, `trigger`, `sources_total`, `sources_done`, `sources_failed`, `created_at`, `started_at`, `completed_at`, plus:

- `children_paused`: how many child runs are currently PAUSED.
- `skipped`: always a list of `{source_id, reason}` for sources the fan-out did not queue. Reasons: `active_run` (a run is already pending/running), `paused_run` (a paused run exists), `login_needed`, `login_failed` (the source's auth realm could not provide a session).

---

## Auth

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/auth/status` | Check if auth is enabled |
| `POST` | `/api/auth/register` | Register a new user (admin-only after first user) |
| `POST` | `/api/auth/login` | Login (email + password) |
| `POST` | `/api/auth/refresh` | Refresh access token |
| `GET` | `/api/auth/me` | Get current user |
| `POST` | `/api/auth/change-password` | Change password |
| `GET` | `/api/auth/keys` | List my API keys |
| `POST` | `/api/auth/keys` | Create an API key |
| `POST` | `/api/auth/keys/{id}/rotate` | Rotate an API key |
| `DELETE` | `/api/auth/keys/{id}` | Revoke an API key |
| `GET` | `/api/auth/admin/keys` | List all API keys (admin-only) |
| `GET` | `/api/auth/users` | List users (admin-only) |
| `GET` | `/api/auth/oauth/{provider}/authorize` | Get OAuth2 authorize URL |
| `GET` | `/api/auth/oauth/{provider}/callback` | OAuth2 callback |

---

## Auth Realms (Admin-only)

Stored credentials for authenticated scraping.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/auth-realms` | Create an auth realm |
| `GET` | `/api/auth-realms` | List auth realms |
| `GET` | `/api/auth-realms/{id}` | Get an auth realm |
| `PATCH` | `/api/auth-realms/{id}` | Update an auth realm |
| `DELETE` | `/api/auth-realms/{id}` | Delete an auth realm |
| `POST` | `/api/auth-realms/{id}/login` | Trigger a login flow |
| `POST` | `/api/auth-realms/{id}/session` | Store a session |
| `POST` | `/api/auth-realms/{id}/test` | Test stored credentials |

---

## Webhooks

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/webhooks` | List webhooks |
| `POST` | `/api/webhooks` | Create a webhook |
| `GET` | `/api/webhooks/{id}` | Get a webhook |
| `PATCH` | `/api/webhooks/{id}` | Update a webhook |
| `DELETE` | `/api/webhooks/{id}` | Delete a webhook |
| `POST` | `/api/webhooks/{id}/test` | Send a test delivery |
| `GET` | `/api/webhooks/{id}/deliveries` | List webhook deliveries |

**Event types** (a webhook subscribes to any subset): `new_page`, `updated_page`, `removed_page`, `extraction_complete`. Payloads are POSTed as JSON, signed with `X-DocExtractor-Signature: sha256=…` (HMAC of the body with the webhook's secret), and retried with backoff.

The `extraction_complete` payload carries a `delta` summary so a consumer knows whether to pull the [Delta Feed](#delta-feed):

```json
{
  "event": "extraction_complete",
  "run_id": "…", "source_id": "…",
  "source_name": "…", "vendor_name": "…", "product_name": "…",
  "delta": { "added": 12, "updated": 3, "removed": 1, "watermark": "eyJ2IjoxLCJzZXEiOjQ4MzB9" }
}
```

The `watermark` is informational; consumers should pull with their own stored cursor.

---

## Dashboard

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/dashboard/overview` | Consolidated per-source overview (status, freshness, articles, last-run, enrichment, **escalation**, `active_run`, job) + aggregates; powers the Dashboard's filter/sort/drill-down |
| `GET` | `/api/dashboard/sources` | Source health scores + counts (legacy; superseded by `/overview` for the Dashboard) |
| `GET` | `/api/dashboard/enrichment` | Per-source + corpus image-enrichment progress (`described`/`pending` per source, `active_run`, and an aggregate); still backs the per-source badge in the Sources list |

---

## Profiles

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/profiles` | List available extraction profiles |

---

## Health

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | Health check (no auth required) |