# Versioned-URL Handling — Design

**Date:** 2026-06-25
**Status:** Approved (design); pending implementation plan
**Goal addressed:** Make it painless to re-extract documentation when a vendor ships a new product version whose version number is embedded in the URLs (Arcserve, Cohesity/NetBackup, Commvault, Dell). Today a new version means deleting and re-entering every source URL by hand, which also destroys the per-topic change history.

## Summary

A product's version is treated as a first-class, **Product-level** value. Each source under that product stores its URL as a template containing a literal `{version}` placeholder; the live `base_url` is that template resolved against the product's current version. Bumping the product version rewrites every child source's URL and re-extracts each one.

History survives the bump because articles are matched **by a version-independent `topic_key`** (the URL with the version token replaced by `{version}`) instead of by their full, version-specific `source_url`. The same topic across `10.0` and `11.0` shares one `topic_key`, so its existing `ArticleVersion` timeline simply continues — a new snapshot and diff are appended at the boundary.

> Bumping a whole product from `10.0` to `11.0` is one field plus a confirm. The source URLs rewrite themselves, every topic's history carries across, dropped topics are flagged removed, and new topics are created — all through the existing incremental-extraction machinery.

## Decisions (locked during brainstorming)

1. **History model:** continuous per-topic timeline (one "current" doc set; old content lives in each article's `ArticleVersion` history). *Not* separately browsable version snapshots.
2. **Bump trigger:** manual, at the **Product** level; it cascades to rewrite all child source URLs.
3. **Version encoding:** explicit `{version}` placeholder template (Approach A), not fuzzy substring find/replace.

## Data model (additive, all nullable)

| Table | Column | Purpose |
|---|---|---|
| `products` | `version` (str, null) | Current version, e.g. `"10.0"`. NULL = product not versioned. |
| `products` | `previous_version` (str, null) | Prior version, to label changelog boundaries and allow bump-back. |
| `documentation_sources` | `url_template` (str, null) | Base URL with a literal `{version}` placeholder. NULL = plain, non-versioned source (today's behavior). |
| `documentation_sources` | `base_url` (existing) | Stays the **resolved, live** URL the extractor uses (= `url_template` with the product version substituted). |
| `articles` | `topic_key` (str) | Version-independent topic identity. Unique `(source_id, topic_key)`. |
| `extraction_runs` | `version` (str, null) | Product version captured at run time; attributes a run and the `ArticleVersion`s it creates to a version. |

Nothing is removed or repurposed; non-versioned sources and all existing queries behave exactly as today.

## Topic key & URL templating (the matching mechanic)

The version token always sits in the **common prefix** that all of a source's article URLs share (it is part of `base_url`, before the per-topic path). Derivation is therefore exact and prefix-anchored — never a naïve global replace:

```
prefix          = url_template text before "{version}"
version         = product.version
topic_key(url)  = prefix + "{version}" + url[len(prefix) + len(version):]
```

Example across a bump:

- `…/Available/10.0/…/install.htm` → `…/Available/{version}/…/install.htm`
- `…/Available/11.0/…/install.htm` → **same key** ✅

`topic_key` is derived from the **persisted `source_url`** — i.e. after any profile-level URL normalization (e.g. Dell's `_to_en_us` forcing `/en-us/` + `lang=en-us`). The stored `url_template` must reflect that normalized form so the prefix matches.

For non-versioned sources (`url_template IS NULL`), `topic_key == source_url`, so the mechanic is a no-op for them.

### Two keyings switch from `source_url` to `topic_key`

Both live in `app/services/firecrawl.py`:

- **`process_article_result`** — upsert on `(source_id, topic_key)` instead of `(source_id, source_url)`. On a bump it finds the prior-version article, **updates its `source_url` to the new URL and appends an `ArticleVersion`** (with diff). The timeline continues.
- **`reconcile_removals`** — compare each current TOC entry's `topic_key` against articles' `topic_key`, so topics dropped in the new version are flagged removed and survivors are re-linked.

## Version-bump flow (product-level)

`POST /api/products/{id}/versions/bump` with `{ "version": "11.0" }`:

1. Validate: the product has `{version}`-templated sources and the new version differs from the current one.
2. Set `product.previous_version = old`, `product.version = "11.0"`.
3. For each templated child source: re-resolve `base_url = url_template.replace("{version}", "11.0")`.
4. Enqueue a normal **incremental extraction run per affected source** (reusing the existing queue/worker), each tagged `version="11.0"`.
5. Each run rebuilds the TOC from the new URL, matches by `topic_key`, appends versions for changed topics, creates articles for new topics, and reconciles removals — the existing incremental machinery, now version-aware via the key.

Failures are per-source: one bad URL fails its own run without blocking the others, same as today.

## Migration & templatizing existing sources

**Schema migration (Alembic):** add the columns above; backfill `articles.topic_key = source_url` for every existing row; add the unique `(source_id, topic_key)` index. Today's upsert already dedupes on `(source_id, source_url)`, so the backfilled keys are already unique and the constraint is safe.

**The bridge from hand-entered URLs to templated ones** is the one risky step and must be done in a single operation. Existing articles have `topic_key = <full 10.0 URL>`; if a `{version}` template were added and the product bumped without re-deriving keys, the next run's `{version}` keys would match nothing and history would break exactly at the boundary we are preserving.

Per product, an **"Enable versioning"** operation:

1. The user enters the current version (`"10.0"`). The system scans each child source's `base_url` for that token and **proposes a `{version}` template** (auto-detect), which the user confirms or hand-edits (for vendors whose format does not literally contain `"10.0"`).
2. On save: set each source's `url_template`, set `product.version`, and **recompute `topic_key` for all existing articles** of those sources using the new template.
3. The first subsequent bump then matches cleanly.

This is idempotent and only touches products the user opts in; everything else stays non-versioned.

## API / routes

- `POST /api/products/{id}/versions/bump` `{ version }` → rewrites templated source URLs, sets product version, enqueues one incremental run per affected source; returns affected sources + run IDs.
- `POST /api/products/{id}/versions/enable` `{ version }` → the templatize + key-backfill flow above.
- `POST /api/sources/{id}/detect-version-token` `{ version }` → returns the proposed `url_template` (server-side detection so the UI confirm step is reliable).
- Source create/update accepts `url_template`; when set, `base_url` is derived from the product version. Validation: a `{version}` template requires the product to have a version.

## Frontend

- **Product view:** show the current version; a **"Bump version"** button → modal (new version) → preview the affected sources and their rewritten URLs → confirm → runs stream progress via the existing run-status UI.
- **Versioning setup:** an "Enable versioning" entry that highlights the detected token in each source URL for confirmation.
- **Source add/edit:** the URL field accepts `{version}` with a live preview of the resolved URL; if the product is versioned and a pasted URL contains the version, offer an inline "use `{version}` here?".
- **History / changelog:** label version snapshots with `run.version` and show a `10.0 → 11.0` boundary marker in the per-article timeline and the consolidated changelog. The side-by-side diff UI already exists; this only labels it.

## Testing

- **Unit — `topic_key` derivation:** Arcserve path version (`/Available/{version}/`), Dell slug version (`pp-dm_{version}_…`), a version string that *also* appears in the topic suffix (proves prefix-anchored replacement), trailing slashes / query strings, and the non-versioned case (`topic_key == source_url`).
- **Unit — template resolve + auto-detect:** `resolve(template, version)` round-trips; token detection finds `"10.0"` and proposes a template; the not-found path returns a clear signal.
- **Integration (sync-DB, mirroring existing tests):** seed a product + two templated sources at `10.0` with articles & versions; bump to `11.0`; assert base_urls rewritten, runs tagged `version="11.0"`, topics matched by `topic_key` (same article rows, `source_url` updated, a new `ArticleVersion` with diff appended), a dropped topic flagged removed, a new topic created. Plus: the templatize backfill prevents a history break, and the migration leaves non-versioned sources unchanged.
- **Edge / error:** bump with no templated sources → 400; same/empty version → 400; `{version}` template with no product version → validation error; one source's bad URL fails its own run without blocking the others.

## Out of scope

- Probing vendors to auto-discover that a new version exists (bump stays manual).
- Keeping old versions as separately browsable / exportable snapshots (history model is the continuous per-topic timeline).
