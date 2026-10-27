# NetBackup source split — design

**Date:** 2026-10-27 · **Status:** approved by owner 2026-10-27 · **Kind:** one-off production data operation (not a feature)

## Why

Cohesity redesigned `docs.cohesity.com`: the left nav (shadcn/radix "collapsible sidebar", profile `collapsible_sidebar`) now lists **only the publication the current page belongs to**, not the whole product. The NetBackup source (`Cohesity NetBackup`, id `cb3a6f6a-f51e-414e-b2ec-64eb9a2217b5`, product `NetBackup`, version `11.2.0.1`) was built when the nav was product-wide: 9,928 live articles across 33 publications. Re-extracted today it would see one publication (Release Notes, 63 pages). The TOC-collapse guard aborts that run (63 < 50% of 9,931), so the corpus is safe but frozen.

Every other source in the system is one TOC = one source. The owner rejected a per-product profile (enumerate publications from the landing page) as a corner case that deviates from that design. Instead: **split the existing source in place into one source per publication**, carrying the corpus over so no article loses its identity.

It also defuses a latent bug. The source's `url_template` is `…/{version}/103228346-{rev}-0/v95650213-{rev}` — the `{rev}` (per-release build id) is anchored on the **Release Notes** publication only. `extract_revision` refuses every other publication's URL, so 9,865 of the 9,928 live `topic_key`s carry their build id as a literal. The next NetBackup release re-mints those ids and would re-key ~9,800 articles (the CommCell duplication class — retire everything, re-add everything). One source per publication gives each its own correctly anchored `{rev}` template.

## Goals

1. Each of the 33 publications becomes its own `DocumentationSource` under the existing `NetBackup` product, extractable by the normal pipeline with no new profile code.
2. Every live article keeps its `Article.id`, `content_markdown`, `content_hash`, images, versions and dates. Downstream (graph-rag) sees an in-place metadata update, never a remove + add.
3. History that the 11.2 → 11.2.0.1 bump orphaned (prior-release copies of live pages, sitting as removed rows) is folded back into the live article's version history, so side-by-side history goes back past 11.2.0.1.
4. The original source is deleted at the end. No archive source remains.

## Non-goals

- No change to the `collapsible_sidebar` profile, the extractor, or the delta feed. (The skeleton-race fix already shipped in PR #228.)
- No UI for splitting sources. This is a one-off script; it is not wired into the API.
- NetBackup Flex Appliance is out of scope (the owner is adding its 8 guides as fresh sources; it has 1 article today).

## Anti-goals (failure even if it "works")

- Any live article getting a new `id`, or downstream seeing it `removed` then `added`.
- `content_changed_at` moving on articles whose content did not change (graph-rag copies it onto the node and uses it as an episode's reference time).
- A moved article served on the delta feed with no chapter (`parent_chapter`/`top_level_chapter` null) because its TOC entry vanished before the feed served it.
- A first extraction of a new source that reports anything other than (nearly) all `unchanged` — that means identity was mis-derived and the run would duplicate.

## Measured inputs (production, 2026-10-27)

All 24,689 article rows of the source, classified. "Normalized key" = the `topic_key` re-derived under the publication's own `{rev}` template.

| Class | Rule | Rows | Versions | Images |
|---|---|---|---|---|
| **live** | `removed_at IS NULL` | 9,928 | 448 | 509 (38 MB) |
| **twin** | removed; its publication is live; its normalized key equals a live article's | 9,995 | 1,445 | 32 (2.3 MB; 30 byte-identical to an image on the live article) |
| **removed-in-live-guide** | removed; publication is live; no live article has its key | 197 | 17 | 0 |
| **retired-guide** | removed; publication not among the 33 live ones (37 retired publication ids) | 4,569 | 468 | 41 (3.9 MB) |

- Twins per live article: 8,265 have one, 865 have two → 9,130 live articles gain history.
- After re-deriving keys, live keys are unique within each publication (0 collisions).
- The 33 live publications match the 33 publications linked from `https://docs.cohesity.com/docs/netbackup/11.2.0.1` one-to-one, with the same build id; no publication has more than one live build id.
- Old copies almost never reference their media: their markdown carries `<Base64-Image-Removed>` placeholders; no twin version's markdown contains a `/media/` path.
- Original source also owns: 14 extraction runs, 9,964 TOC entries, 29,076 `content_changes` rows; 0 export jobs, 0 webhooks, 0 TOC checkpoints, no job/schedule.

FKs that matter (from `pg_constraint`): `articles.{extraction_run_id,created_run_id,removal_run_id}` and `article_versions.extraction_run_id` → runs are **SET NULL**; `articles.toc_entry_id` → TOC **SET NULL**; `article_images`/`article_versions` → articles **CASCADE**; everything under a source **CASCADE**; `content_changes` has no FKs.

## The operation

Three phases, run in order, each gated on the previous one's checks.

### Phase 1 — split (one transaction)

1. **Create 33 sources** under the `NetBackup` product. Per publication:
   - `name` = the publication title from the landing page (e.g. "NetBackup™ Release Notes").
   - `base_url` = the publication root `https://docs.cohesity.com/docs/netbackup/11.2.0.1/<pub>-<build>-0` (serves its first topic directly; verified on Flex that `templatize_url` and `extract_revision` work on root URLs).
   - `url_template` = `CollapsibleSidebarProfile().templatize_url(base_url, "11.2.0.1")` → `…/{version}/<pub>-{rev}-0`.
   - `platform = "collapsible_sidebar"`, `source_type = "web"`; copy any other config the original carries (auth realm: none).
2. **Move live articles**: set `source_id` to the publication's new source and `topic_key = derive_topic_key(source_url, new_template, "11.2.0.1")`.
2b. **Move TOC entries with them.** The feed's `ChapterResolver` (`delta_feed.py`) only loads TOC entries whose `source_id` equals the article's *current* source, so leaving entries on the original source would serve all 9,928 `updated` records with null `parent_chapter`/`top_level_chapter` (anti-goal) until each publication's phase-2 run — days, given sequential runs. So re-assign each original TOC entry to a new source: an entry with a URL goes to its URL's publication; a URL-less entry goes to the single publication all its descendants belong to. An entry whose descendants span several publications, or none (e.g. a product-level root), stays on the original source and is deleted in phase 3 (its children's `parent_id` → NULL by FK, so they become roots of their publication's tree — which is what the per-publication sidebar shows anyway). `toc_entry_id` on moved articles then stays valid. Phase 2 rebuilds each TOC from the live sidebar regardless. (Builder's call, raised by the design review.)
3. **Fold twins into history**: for each live article, take its twins (oldest first by `extracted_at`); emit, in chronological order, each twin's own `article_versions` followed by one version for the twin row itself (`content_markdown`, `content_hash`, `source_url`, `extracted_at` = the twin's). Re-point rather than copy where possible (`UPDATE article_versions SET article_id = <live id>`). Then delete the twin rows (their images cascade). Skip a version whose `content_hash` equals the immediately preceding one in the merged chain.
4. **Move removed-in-live-guide rows** to their publication's source as removed rows, `topic_key` re-derived the same way. (Designer's call, shown to the owner with the impact numbers and not objected to: they are ordinary removal history of a current guide.)
5. **Delete retired-guide rows** (versions/images cascade). They were tombstoned on the feed when removed (2026-07-12 … 2026-09-16); nothing new is emitted.
6. **Outbox**: one `content_changes` row per moved live article — `change_type = 'updated'`, new `source_id`, new `topic_key`, the article's `content_hash`, `run_id = NULL` — written **directly**, not through `change_log.record_change`, so `content_changed_at`/`content_changed_basis` are not stamped (a move is not a content mutation). Take the `_DELETION_LOCK_KEY` advisory lock first, like other out-of-band outbox writers, so outbox ids cannot interleave with a concurrent out-of-band commit.

Preconditions, checked inside the transaction before any write: no `extraction_runs` row in a non-terminal status anywhere (the feed's safe ceiling is driven by active runs; also the owner's standing rule); the source's live count and class counts still match a fresh classification (abort on drift).

Postconditions, checked before `COMMIT` (abort → rollback on any failure): original source has 0 live articles and 0 rows other than removed-in-live-guide that were not moved; Σ live articles across the 33 sources = 9,928; no `(source_id, topic_key)` duplicate; every live article that had twins has ≥ 1 new version; outbox rows written = live articles moved.

### Phase 2 — first extraction of each new source

Trigger a normal `extract` run per new source, **sequentially** (the owner's rule: no parallel Firecrawl/Browserless load). Requires PR #228 deployed (merged; CI running at time of writing). Expected per run: TOC rebuilt from that publication's sidebar, every page `unchanged` (or a genuine vendor update since 2026-09-16), `toc_entry_id` re-linked to the new source's TOC. **Stop the sequence** on the first run whose `articles_new` **or** `articles_updated` exceeds a small threshold, or that fails, and investigate before continuing. `articles_new` catches an identity mismatch; `articles_updated` catches a content-hash drift, which would archive a version and move `content_changed_at` on every page (anti-goal). Note that the run's `source_url`/LIKE fallbacks can heal a key mismatch silently, so a low `articles_new` alone is not proof the keys are right; the dry run's key check (Safety) is.

Hash stability was measured before the run: 40 random live NetBackup articles re-fetched on 2026-10-27 through the exact raw-HTTP pipeline (`fetch_raw` → `extract_content_html` → `html_to_markdown` → `sanitize_markdown` → `_normalize_for_change_hash`) reproduced the stored `content_hash` **40/40**. 9,923 of the live articles were last extracted 2026-09-16.

### Phase 3 — delete the original source

Only after all 33 phase-2 runs completed. Delete through the normal route (`DELETE /api/sources/<id>`), which emits tombstones only for live articles — there are none left, so the feed gets nothing. Runs, TOC entries and its remaining rows cascade. Run history and the original source's per-source changelog are lost (accepted by the owner; the outbox rows survive, keyed by the old `source_id`). Graph-rag keeps a stale node for the old Source (cosmetic).

## Downstream: graph-rag

**Decision (owner, 2026-10-27): no graph-rag change.** Graph-rag extraction has not run for NetBackup yet, so there are no NetBackup episodes or wiring to keep correct; when it does run it will read the new per-publication sources. The analysis below is kept for any future source move of a corpus graph-rag *has* ingested.

Verified in `~/Documents/Shared/projects/graph-rag` (`src/graph_sync/sync_core.py::_apply_record`): it does key Article nodes by `id` (`MERGE (a:Article {id})`), and its incremental poll is unfiltered (`since=cursor` only), so it will *see* the split's rows. But its **hash gate runs first**: when the node's stored `content_hash` equals the record's, the record is skipped entirely — no `apply_structural`, no source re-wiring, no TOC sync for the touched source. Every split row has an unchanged hash by construction, so graph-rag would ignore all 9,928 of them. The articles stay wired to the original Source node (and its chapters) and, after phase 3, hang off a source that no longer exists — not lost, not duplicated, but mis-wired until each page's content next changes.

Options:
- **(a) graph-rag change** (recommended): when the hash matches but `source_id` or `topic_key` differ, apply the structural update without enqueuing a semantic (LLM) job. Small, and it makes any future source move correct.
- **(b) force re-application**: clear graph-rag's stored hash for the 9,928 articles so they re-apply — this also re-runs semantic extraction (LLM cost across ~10k articles).
- **(c) accept** the stale wiring and let it heal page by page. ← chosen (no NetBackup data in graph-rag yet).

Whichever is chosen must be in place before phase 1's outbox rows are consumed (pause graph-rag's poll across phase 1 if the change ships later).

## Safety

- **Backup** before phase 1: `pg_dump` of the affected tables restricted to this source's rows (articles, article_versions, article_images, toc_entries, extraction_runs, content_changes) — or a full dump if simpler — kept outside the cluster.
- **Rehearsal**: restore a production snapshot into the local Docker Postgres (`docextractor-pg`, `localhost:5432`), run phase 1 there with the same script, run the postconditions, and diff the classification. Phase 2 cannot be rehearsed without the full stack; instead, the script's dry run (below) must reproduce, for a sample of live articles, exactly the `topic_key` a run would derive (`derive_topic_key(source_url, template, version)` is the same function the run calls).
- **Dry-run mode** (default): compute everything, print the plan and counts, write nothing. `--apply` required to commit.
- **Destructive-write authorization**: the owner must explicitly OK the production `--apply` and the phase-3 delete (standing rule).
- The alembic CLI and tests target the local Docker DB on this machine; the script must take its DB URL explicitly and print the target host/db before doing anything.

## Decided by the builder

- Where the script lives (`backend/scripts/` one-off, committed for the record) and its exact CLI.
- How the 33 publication titles/root URLs are obtained (fetch the landing page at run time, or embed the list captured during the rehearsal — embedding is fine and avoids network in the transaction).
- Batch sizes / statement shape (Core executemany per CLAUDE.md's guidance on ORM bulk updates).
- `sort_order` of the new sources and of moved articles (phase 2 rewrites article order anyway).
- `diff_text` on folded versions: leave NULL unless cheap to compute consistently with `firecrawl.py`'s versioning.
- Threshold for "small" `articles_new` in phase 2 (suggest ≤ 1% of that publication's live count).
