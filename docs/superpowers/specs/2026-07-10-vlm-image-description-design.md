# Design — VLM Image-Description Enrichment

- **Date:** 2026-07-10
- **Status:** Approved (design); implementation pending
- **Scope:** Spec 2 of 2. Depends on Spec 1
  (`2026-07-10-graphrag-delta-feed-design.md`) at the record schema: this spec
  populates `images[].description` and `images[].kind`, which the delta feed emits.

## Purpose

Documentation is heavily visual — screenshots of UI flows, architecture diagrams,
topology charts. A text-only graph RAG pipeline is blind to that content. This spec
adds VLM-generated natural-language descriptions of the **meaningful** images
(ignoring icons/spacers/thumbnails) so that:

1. Text-only GraphRAG ingestion picks the descriptions up automatically (they are
   written inline into the article markdown as captions), and
2. Multimodal / precision consumers can read them as structured fields
   (`images[].description`, `images[].kind`).

## Decisions (settled during brainstorming)

- **Placement:** inline in markdown **and** structured field. The description is written
  into `content_markdown` near the image (so text RAG sees it with zero connector work)
  and also exposed as `images[].description` / `kind`.
- **When:** a **dedicated enrichment phase at the end of the extraction run** (after
  scraping, before the run completes), reading the images already downloaded to disk —
  **not** inline in `process_article_result`. (Revised during planning: `content_hash` is
  computed early in `process_article_result`, before images are downloaded, so unchanged
  pages fast-path without any download — load-bearing for large raw-HTTP sources like
  Rubrik's 4,240 pages. Injecting captions "before the hash" would entangle that
  hot path and risk regressing incremental runs. A post-scrape phase keeps the
  change-detection path untouched.) The phase updates `content_markdown` + `content_hash`
  and writes an `updated` `content_changes` row for each enriched article, so the delta
  feed delivers the descriptions. Accepted trade-offs: a net-new enriched article emits an
  `added` (pre-caption) then an `updated` (captioned) row in the same run — both idempotent
  upserts downstream; and the run's `updated` count is slightly inflated. VLM cost/latency
  is bounded by a per-run budget + the `bytes_sha256` cache.

## Existing primitives this builds on

- **Image download + boilerplate filter** already exist in `FirecrawlService`
  (`_download_image`, `_BOILERPLATE_IMG_RE` rejecting skins / `/ui-icons/` / spacers) and
  `ArticleImage` rows (`original_url`, `local_filename`, `alt_text`, `file_size_bytes`,
  `sort_order`).
- **VLM-over-OpenRouter pattern** already exists in `app/services/pdf_escalate.py`
  (`escalate_segments`): a per-run page/percentage budget, a consecutive-failure circuit
  breaker, `settings.pdf_vlm_*` config (base_url, api_key, model `qwen/qwen3-vl-32b-instruct`),
  and heartbeat-safe offloading via `asyncio.to_thread`. This spec mirrors that shape with an
  independent config block.
- **Heartbeat lesson** (PR #90, `worker-event-loop-heartbeat`): long synchronous work on the
  event loop starves the run heartbeat and gets the run reaped. All CPU/network work here is
  async or offloaded with `asyncio.to_thread`.

## Selection — which images are "meaningful"

A layered filter, cheapest checks first, so the VLM only ever sees images worth describing.
Applied in the enrichment phase over the `ArticleImage` rows of the articles touched this
run, reading each image's bytes from disk (`media_dir/<article_id>/<local_filename>` — no
re-download):

1. **Boilerplate reject** — reuse `_BOILERPLATE_IMG_RE` (skins, ui-icons, spacers).
2. **Dimension / size reject** — capture `width` / `height` while the bytes are in hand.
   This **adds `Pillow` as a new backend dependency** (the standard Python imaging library;
   not currently in `requirements.txt`), used only to read image dimensions via
   `Image.open(BytesIO(bytes)).size`. Skip anything below a threshold
   (`width < settings.image_min_dimension or height < settings.image_min_dimension`, default
   100 px) or below `settings.image_min_bytes` (default ~3 KB). Kills icons, bullets,
   thumbnails, tracking pixels.
3. **Cross-source dedup** — compute `bytes_sha256` of the image bytes. A logo/banner repeated
   across pages is described **once**; every occurrence reuses the cached description. This is
   also the cost cache (see below).

Survivors are marked `is_meaningful = true` and queued for description within the run.
Everything else keeps `is_meaningful = false`, no description, and its markdown reference is
left untouched.

## Description service — `app/services/image_describe.py`

Mirrors `pdf_escalate` structure, with its own config so image and PDF budgets tune
independently:

```
settings.image_vlm_enabled: bool = False          # opt-in
settings.image_vlm_base_url: str  = "https://openrouter.ai/api/v1/chat/completions"
settings.image_vlm_api_key: str   = ""             # Bearer key (env only)
settings.image_vlm_model:   str   = "qwen/qwen3-vl-32b-instruct"
settings.image_vlm_max_per_run: int = 100          # budget: max NEW descriptions per run
settings.image_vlm_max_consecutive_failures: int = 5   # circuit breaker
settings.image_min_dimension: int = 100
settings.image_min_bytes: int = 3072
```

`describe_image(image_bytes, alt_text) -> ImageDescription | None`:

- POSTs an OpenAI-compatible vision chat-completions request to `image_vlm_base_url` with the
  image as a base64 `data:` URL and a retrieval-tuned prompt:

  > "You are describing an image from software product documentation so it can be found by
  > search. If it is a screenshot, state which screen/dialog it shows and the action or state
  > depicted. If it is a diagram or chart, name the components and their relationships or the
  > trend shown. Be concise (1–3 sentences). Ignore window chrome, browser frames, and
  > decorative borders. Then classify it as one of: screenshot, diagram, chart, photo, other."

- Returns `ImageDescription(text: str, kind: Literal["screenshot","diagram","chart","photo","other"])`.
- Returns `None` on a service failure (so the caller's circuit breaker can distinguish a
  genuine failure from a valid description) — same contract as `escalate_segment`.

`describe_article_images(...)` — the per-article driver, called by the enrichment phase:

- Iterates the article's meaningful, not-yet-described images (reading bytes from disk).
- **Cache first**: look up `image_descriptions` by `bytes_sha256`; on hit, reuse (no VLM call).
- On miss, and within `image_vlm_max_per_run` budget and the circuit breaker, call
  `describe_image`, persist to the cache, attach to the `ArticleImage` row.
- Bounded + circuit-broken exactly like `escalate_segments`; never raises into extraction
  (best-effort — a VLM outage degrades to no descriptions, not a failed run).

## Storage

`ArticleImage` gains columns: `description Text null`, `kind String(16) null`,
`width Integer null`, `height Integer null`, `is_meaningful Boolean default false`,
`bytes_sha256 String(64) null` (indexed).

New table `image_descriptions` — the content-hash cache, shared across all articles/sources:

| column          | type            | purpose                                   |
|-----------------|-----------------|-------------------------------------------|
| `bytes_sha256`  | `String(64)` PK | image content hash (the cache key)        |
| `description`   | `Text`          | VLM description                           |
| `kind`          | `String(16)`    | classification                            |
| `model`         | `String(128)`   | which model produced it (for later re-runs) |
| `created_at`    | timestamptz     | audit                                     |

## Placement in markdown

After an image's description is resolved, inject a caption block immediately after the image
reference in `content_markdown`, deterministically:

```markdown
![topology](/media/<article-id>/x.png)

> **Figure:** Topology diagram showing the backup proxy connecting the vSphere host to the
> repository over the management network.
```

Injection is idempotent: keyed on the served image URL, it replaces an existing
`> **Figure:** …` block for that image rather than appending a second one. Same image + same
description → byte-identical markdown.

## Interaction with Spec 1 — keeping the delta feed honest

Injecting a caption changes `content_markdown` and `content_hash`. The enrichment phase runs
after scraping (so `process_article_result` has already written its `added`/`updated` row with
pre-caption content), then, for each enriched article, updates `content_markdown` +
`content_hash` and writes its own `updated` `content_changes` row so the descriptions reach the
feed. Two rules keep this honest:

1. **The phase only acts on articles with meaningful, not-yet-described images.** An article
   with no meaningful images, or whose meaningful images are already described (cache hit
   yielding the identical caption), produces **byte-identical** `content_markdown` → identical
   `content_hash` → the phase writes **no** `updated` row. So repeated runs over unchanged
   content emit nothing.
2. **Descriptions are cached by `bytes_sha256`** (the `image_descriptions` table), shared across
   all articles/sources. VLM cost is paid once per distinct image, ever; a re-extracted or
   re-used image reuses its cached caption.

The phase does **not** create an `ArticleVersion` for the caption injection (captions are a
derived enrichment, not a new source revision; the prior content — if any — was already
snapshotted by `process_article_result` on the real change). It updates the live article in
place and emits the outbox row.

Net steady-state: incremental runs pay VLM cost only for genuinely new or changed images, and
the feed emits an `updated` for an article only when its description set actually changes.

## Error handling

- **VLM/service failure**: `describe_image` returns `None`; the driver counts it toward the
  circuit breaker and moves on. The image keeps no description; markdown is left untouched.
  Never fails the run.
- **Budget exhausted**: remaining meaningful images this run are left undescribed (and
  un-injected); they are picked up on a later run (they remain `is_meaningful=true`,
  description `null`). Because caption injection is gated on having a description, an
  undescribed image does not alter markdown/hash — so deferring it does not churn the feed.
- **Non-image / corrupt bytes**: Pillow open fails → treated as not meaningful, skipped.
- **Model change** (`image_vlm_model` updated later): cache rows record `model`; a future
  backfill can re-describe stale-model entries. Out of scope here.

## Testing

- Selection: boilerplate URL rejected; sub-threshold dimension/bytes rejected; a real
  screenshot (large, non-boilerplate) selected; duplicate bytes across two pages selected once
  and reused.
- `describe_image`: happy path returns text+kind; service error returns `None`; base64/data-URL
  request shape asserted against a mocked endpoint.
- Driver: budget cap honored; consecutive-failure circuit breaker trips and stops calling;
  best-effort (a raised VLM error does not propagate into extraction).
- Cache: second occurrence of the same `bytes_sha256` hits the cache and makes **no** VLM call.
- **Idempotence / no phantom delta**: describing an article, then re-running with unchanged
  images, yields byte-identical `content_markdown`, identical `content_hash`, and **no**
  `content_changes` `updated` row (the cross-spec invariant).
- Surfacing: `images[].description` / `kind` appear in `GET /api/articles/{id}` and in the
  Spec 1 JSONL delta record; the inline caption appears in `content_markdown`.

## Out of scope (this spec)

- Re-describing cached images after a model upgrade (backfill).
- Multimodal embedding of the raw image bytes (the downstream GraphRAG project's concern).
- Describing PDF-sourced figures (the PDF path already has its own VLM escalation; extending
  image descriptions to `pdf_images` can be a later increment).
