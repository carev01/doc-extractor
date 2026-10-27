# NetBackup Source Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the `Cohesity NetBackup` source (id `cb3a6f6a-f51e-414e-b2ec-64eb9a2217b5`) in place into 33 per-publication sources, folding orphaned prior-release copies into live articles' version history, then re-extract each and delete the original.

**Architecture:** One self-contained one-off module, `backend/app/ops/netbackup_split.py`: a read-only planner (`build_split_plan`), a single-transaction applier with pre/postconditions (`apply_split`), and a CLI whose dry run executes the real apply and rolls back. A second small module drives phase 2's sequential runs with the stop rule. Production runs happen inside the worker pod (`kubectl cp` + `PYTHONPATH=/app`), which already has the DB URL, so no port-forward is needed.

**Tech Stack:** Python 3.13, SQLAlchemy 2 async (asyncpg), httpx, pytest + pytest-asyncio against `docextractor_test` (local Docker PG `docextractor-pg`).

**Spec:** `docs/superpowers/specs/2026-10-27-netbackup-source-split-design.md`

## Global Constraints

- Version string: `"11.2.0.1"` (read from the product row, `products.version`; abort if it differs).
- New-source fields: `name` = guide title; `base_url` = `https://docs.cohesity.com/docs/netbackup/11.2.0.1/<pub>-<build>-0`; `url_template` = `CollapsibleSidebarProfile().templatize_url(base_url, version)`; `platform="collapsible_sidebar"`; `source_type="web"`; `profile_config` and `auth_realm_id` copied from the original; same `product_id`.
- Identity: always `versioning.derive_topic_key(url, new_template, version)` — never a hand-rolled regex. `url` = the article's `source_url` for **live** rows; for **removed** rows, `topic_key.replace("{version}", version)` (their `source_url` still carries the old `11.2` segment).
- Publication of a URL: `re.search(r"/docs/netbackup/[^/]+/(\d+)-(\d+)-\d+", url)` group 1. A live article whose publication is not one of the 33 guides → abort, never guess.
- Outbox rows for moved live articles: `ContentChange(change_type="updated", run_id=None, source_id=<new>, topic_key=<new>, content_hash=article.content_hash)`, written directly — **not** via `change_log.record_change` (must not stamp `content_changed_at`). Take `pg_advisory_xact_lock(change_log._DELETION_LOCK_KEY)` before writing.
- Bulk updates by primary key via Core `executemany` on `Table` objects (CLAUDE.md: ORM `update(Article)` with a parameter list cannot key on non-PK columns and has bitten before).
- Phase-2 stop rule: `articles_new > 1%` or `articles_updated > 1%` of that source's live count (minimum threshold 1 article), or run status `failed`/`cancelled`.
- Never run against production without the owner's explicit OK for `--apply` and for the phase-3 delete. The CLI prints the target host/db first.
- Commit footer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Re-running `--apply` after it already succeeded** → must abort before writing (the original source has 0 live articles / a source with a guide's name already exists under the product), not create 33 more sources. → test in Task 2.
2. **An extraction run active (status `pending`/`running`/`queued`) anywhere** when apply starts → abort, nothing written. → test in Task 2.
3. **Two removed-in-live-guide rows that derive the same new key** (removed in both eras) → would violate `uq_articles_source_topic`; keep the newest as the row and fold the older into its versions, exactly like twins. → test in Task 1 + Task 2.
4. **Consecutive identical `content_hash` in a merged chain** (19 twins equal their live article) → no duplicate version row. → test in Task 2.
5. **The delta feed after apply** → serves each moved article as `updated` with the new `source_id`, new `topic_key`, non-null `parent_chapter`, and `content_changed_at` unchanged. → test in Task 2.

---

### Task 1: Read-only split planner

**Files:**
- Create: `backend/app/ops/__init__.py` (empty), `backend/app/ops/netbackup_split.py`
- Test: `backend/tests/test_netbackup_split.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Guide: pub: str; build: str; title: str`
  - `GUIDES: tuple[Guide, ...]` — the 33 guides, embedded from `/tmp/claude-1000/-home-openclaw-Documents-Shared-projects-doc-extractor/3dc54d52-c2c5-42fa-a9d6-42d2446bce12/scratchpad/flex/nb_guides.txt` (format `pub|build|variant|title`; variant is always `0`). If that file is gone, re-derive from `https://docs.cohesity.com/docs/netbackup/11.2.0.1` (first link per `/docs/netbackup/11.2.0.1/<pub>-<build>-<variant>`).
  - `@dataclass class NewSource: pub: str; name: str; base_url: str; url_template: str`
  - `@dataclass class SplitPlan:` `version: str`, `product_id: UUID`, `sources: dict[str, NewSource]` (by pub), `live: dict[UUID, tuple[str, str]]` (article id → (pub, new_key)), `history: dict[UUID, list[UUID]]` (surviving article id → rows to fold into it, **oldest `extracted_at` first**), `removed_moves: dict[UUID, tuple[str, str]]` (removed-in-live-guide survivors → (pub, new_key)), `retired: list[UUID]`, `toc: dict[UUID, str | None]` (toc entry id → pub, or None = stays and is deleted in phase 3), and `def counts(self) -> dict[str, int]` (keys: `live`, `folded`, `removed_moves`, `retired`, `toc_moved`, `toc_left`).
  - `async def build_split_plan(db: AsyncSession, source_id: UUID, guides: Sequence[Guide] = GUIDES) -> SplitPlan` — reads only.
  - `class SplitAborted(Exception)`.

Classification rules (the spec's table): live = `removed_at IS NULL`; for a removed row, if its pub is not a guide pub → `retired`; else derive its key; if a live article in that pub has it → fold into that live article (`history`); else it is removed-in-live-guide — group those by key, the newest survives into `removed_moves`, older ones fold into it (`history`). TOC: an entry with a URL → its pub; URL-less → the single pub of all its descendants' URLs, else None.

- [ ] **Step 1: Write the failing tests** in `tests/test_netbackup_split.py`, using the async harness pattern of `tests/test_cascading_deletes.py` (drop_all/create_all on `docextractor_test`, NullPool). A fixture `corpus` builds: vendor/product (`version="11.2.0.1"`), the original source (template `https://docs.cohesity.com/docs/netbackup/{version}/103228346-{rev}-0/v95650213-{rev}`), and two guides `Guide("103228346","173151032","Release Notes")`, `Guide("44037985","173488551","Status Codes")` passed explicitly (not `GUIDES`). Articles (URLs follow the real grammar):
  - `L1` live, Status Codes, `…/11.2.0.1/44037985-173488551-0/v15098603-173488551`, topic_key with literal build.
  - `T1a` removed (older) + `T1b` removed (newer) twins of L1: topic_key `…/{version}/44037985-170000001-0/v15098603-170000001` and `-170000002-`, `extracted_at` T1a < T1b; T1a has one ArticleVersion.
  - `L2` live, Release Notes, topic_key already `{rev}` form.
  - `R1old`, `R1new` removed, Status Codes, same topic `v15000000` at two builds, no live article.
  - `X1` removed, publication `99999999` (retired).
  - TOC: heading `H` (no URL) → child entry with L1's URL; heading `ROOT` (no URL) → children in both guides.
  ```python
  async def test_plan_classifies_every_row(corpus):
      plan = await build_split_plan(db, corpus.source_id, corpus.guides)
      assert plan.live[corpus.L1] == ("44037985", "https://docs.cohesity.com/docs/netbackup/{version}/44037985-{rev}-0/v15098603-{rev}")
      assert plan.history[corpus.L1] == [corpus.T1a, corpus.T1b]
      assert plan.removed_moves[corpus.R1new][0] == "44037985" and plan.history[corpus.R1new] == [corpus.R1old]
      assert plan.retired == [corpus.X1]
      assert plan.toc[corpus.H] == "44037985" and plan.toc[corpus.ROOT] is None
      assert plan.sources["44037985"].url_template == "https://docs.cohesity.com/docs/netbackup/{version}/44037985-{rev}-0"
  async def test_new_key_equals_what_a_run_would_derive(corpus): ...  # derive_topic_key(L1.source_url, template, "11.2.0.1") == plan.live[L1][1]
  async def test_live_article_outside_the_guides_aborts(corpus): ...  # add a live article in pub 88888888 → SplitAborted
  async def test_version_mismatch_aborts(corpus): ...  # product.version="11.3" → SplitAborted
  ```
- [ ] **Step 2: Run** `cd backend && .venv/bin/python -m pytest tests/test_netbackup_split.py -v -p no:cacheprovider` — expect FAIL (`ModuleNotFoundError: app.ops`).
- [ ] **Step 3: Implement** `Guide`, `GUIDES`, `NewSource`, `SplitPlan`, `SplitAborted`, `build_split_plan` in `backend/app/ops/netbackup_split.py`. Load rows with plain selects (id, source_url, topic_key, removed_at, extracted_at; toc id/url/parent_id); classify in Python.
- [ ] **Step 4: Run** the same command — expect PASS.
- [ ] **Step 5: Commit** `feat(ops): read-only planner for the NetBackup source split`.

### Task 2: Single-transaction applier

**Files:**
- Modify: `backend/app/ops/netbackup_split.py`
- Test: `backend/tests/test_netbackup_split.py`

**Interfaces:**
- Consumes: `SplitPlan`, `build_split_plan`, `SplitAborted` (Task 1).
- Produces: `@dataclass class SplitResult: source_ids: dict[str, UUID]; moved: int; versions_added: int; versions_repointed: int; deleted: int; toc_moved: int; outbox: int` and `async def apply_split(db: AsyncSession, source_id: UUID, plan: SplitPlan) -> SplitResult` — performs every write and checks postconditions **without committing**; raises `SplitAborted` on any failed check (caller rolls back).

Order inside `apply_split`:
1. Preconditions: advisory lock; no `ExtractionRun` with a non-terminal status (reuse `delta_feed._ACTIVE`); original source has ≥ 1 live article; no source under the product already named like a guide; re-run `build_split_plan` and compare `counts()` (drift → abort).
2. Insert the 33 `DocumentationSource` rows (fields per Global Constraints).
3. TOC: `UPDATE toc_entries SET source_id` per entry in `plan.toc` with a pub; then set `parent_id = NULL` on every moved entry whose parent did not move to the same source (a cross-guide root left behind) — otherwise `ChapterResolver` walks to a parent it cannot see and serves `top_level_chapter = null`.
4. Fold history: for each survivor, walk its `history` list oldest first; for each folded row, re-point its `article_versions` (`UPDATE … SET article_id = survivor`), then add one `ArticleVersion` built from the folded row (`content_markdown`, `content_hash`, `source_url`, `extracted_at`, `extraction_run_id`) unless its `content_hash` equals the previous version in the merged chain (by `extracted_at`) or the survivor's current hash; then delete the folded rows.
5. Move live articles and `removed_moves` (`source_id`, `topic_key`) — Core executemany.
6. Delete `plan.retired` rows.
7. Outbox: one `updated` `ContentChange` per moved live article (not for removed moves).
8. Postconditions: original has 0 live; Σ live over new sources == `len(plan.live)`; no `(source_id, topic_key)` duplicate across the new sources; every survivor in `history` with a live key has ≥ 1 version; outbox rows written == `len(plan.live)`; every moved live article's `content_changed_at` equals its pre-split value.

- [ ] **Step 1: Write the failing tests** (same fixture):
  ```python
  async def test_apply_moves_folds_and_drops(corpus): ...
      # after apply_split + commit: L1.source_id == result.source_ids["44037985"]; L1.id unchanged;
      # T1a/T1b/R1old/X1 gone; L1 has 3 versions ordered T1a's own, T1a, T1b; R1new moved, still removed, 1 version;
      # original source: 0 live articles; ROOT still on it; H moved
  async def test_apply_does_not_touch_content_changed_at(corpus): ...
  async def test_identical_consecutive_hash_is_not_duplicated(corpus): ...  # T1b.content_hash = L1.content_hash → L1 gets 2 versions, not 3
  async def test_apply_refuses_when_a_run_is_active(corpus): ...  # add ExtractionRun(status="running") → SplitAborted, nothing written after rollback
  async def test_second_apply_aborts(corpus): ...  # apply+commit, then build_split_plan/apply again → SplitAborted
  async def test_delta_feed_serves_the_move(corpus, client): ...
      # GET /api/articles/delta?since=<cursor before apply> → exactly one record for L1:
      # change_type "updated", source_id new, topic_key new, parent_chapter == "H", content_changed_at unchanged; no records for removed/retired rows
      # and L2 (under ROOT's Release Notes child, ROOT left behind): top_level_chapter == that child's title, not None
  ```
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_netbackup_split.py -v -p no:cacheprovider` — expect the new tests FAIL (`apply_split` undefined).
- [ ] **Step 3: Implement** `SplitResult` and `apply_split` per the order above.
- [ ] **Step 4: Run** the same command — expect all PASS. Then `pytest tests/test_delta_feed.py tests/test_change_log.py -p no:cacheprovider -q` — expect PASS (untouched).
- [ ] **Step 5: Commit** `feat(ops): single-transaction NetBackup split applier with pre/postconditions`.

### Task 3: CLI with a real dry run

**Files:**
- Modify: `backend/app/ops/netbackup_split.py`
- Test: `backend/tests/test_netbackup_split.py`

**Interfaces:**
- Consumes: `build_split_plan`, `apply_split`.
- Produces: `async def run(db_url: str, source_id: UUID, apply: bool) -> SplitResult` and `if __name__ == "__main__":` argparse with `--db-url` (default `settings.database_url`), `--source-id` (default `cb3a6f6a-f51e-414e-b2ec-64eb9a2217b5`), `--apply` (flag). Prints the target `host/db` first, then `plan.counts()`, then the `SplitResult`; **dry run executes `apply_split` and rolls back**, so every postcondition is exercised without writing. Exit code 1 on `SplitAborted`.

- [ ] **Step 1: Write the failing test** `test_dry_run_writes_nothing(corpus)`: `await run(TEST_DATABASE_URL, corpus.source_id, apply=False)` returns a `SplitResult` with `moved == 2`, and afterwards the original source still has its live articles and no new sources exist.
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement** `run` + `__main__`.
- [ ] **Step 4: Run** the whole file — expect PASS.
- [ ] **Step 5: Commit** `feat(ops): netbackup_split CLI, dry run = apply + rollback`.

### Task 4: Phase-2 sequential runner

**Files:**
- Create: `backend/app/ops/split_phase2.py`
- Test: `backend/tests/test_split_phase2.py`

**Interfaces:**
- Produces: `async def extract_sequentially(client: httpx.AsyncClient, source_ids: Sequence[str], live_counts: Mapping[str, int], *, ratio: float = 0.01, poll_s: float = 30.0) -> list[dict]` — for each source: `POST /api/extraction/trigger/{id}`, poll `GET /api/extraction/runs/{run_id}` until `status` is terminal, append the run dict; stop (return what ran so far, last item included) when the stop rule fires. `async def main()` reads `DOCEXTRACTOR_API_URL` (default `http://docextractor.k3s.home.lan`) and `DOCEXTRACTOR_API_KEY` (sent as `X-API-Key`), lists the product's sources via `GET /api/sources?product_id=<id>&limit=200` and live counts via the source detail or `/api/articles` count, skips the original source.
- Stop rule: `threshold = max(1, round(ratio * live_counts[id]))`; stop if `articles_new` (None → 0) `> threshold` or `articles_updated > threshold` or status in `{"failed", "cancelled"}`.

- [ ] **Step 1: Write the failing tests** with `httpx.MockTransport`: `test_runs_all_when_clean` (3 sources, each run `completed` with 0 new/0 updated → 3 results, triggers in order), `test_stops_on_new_articles` (second run `articles_new=50` of 1,000 live → 2 results, third never triggered), `test_stops_on_updated_articles`, `test_stops_on_failed_run`. `poll_s=0`.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_split_phase2.py -v -p no:cacheprovider` — expect FAIL.
- [ ] **Step 3: Implement** `extract_sequentially` and `main`.
- [ ] **Step 4: Run** — expect PASS; then the full suite `pytest -q -p no:cacheprovider` — expect exit code 0.
- [ ] **Step 5: Commit** `feat(ops): sequential phase-2 runner with stop rule`; push branch, open PR (spec + plan + ops modules; inert in production).

### Task 5: Rehearsal on a production snapshot (local)

No code. Deliverable: a dry-run and an applied run on a local copy, both clean.

- [ ] **Step 1:** Check prod DB size: `kubectl -n docextractor exec docextractor-postgres-0 -- psql -U docextractor -d docextractor -Atc "select pg_size_pretty(pg_database_size('docextractor'))"`.
- [ ] **Step 2:** `kubectl -n docextractor exec docextractor-postgres-0 -- pg_dump -U docextractor -Fc docextractor > <scratchpad>/prod.dump`; `createdb -h localhost -U docextractor docextractor_rehearsal`; `pg_restore -h localhost -U docextractor -d docextractor_rehearsal --no-owner <scratchpad>/prod.dump`. Keep `prod.dump` as the phase-1 backup (it is taken with no run active).
- [ ] **Step 3:** Dry run: `.venv/bin/python -m app.ops.netbackup_split --db-url postgresql+asyncpg://docextractor:docextractor_dev@localhost:5432/docextractor_rehearsal` — expect counts ≈ live 9,928 / folded ≈ 9,995 (+ older removed-in-live duplicates) / removed_moves ≤ 197 / retired 4,569, and a `SplitResult` with no `SplitAborted`.
- [ ] **Step 4:** Same with `--apply`; then spot-check with SQL on `docextractor_rehearsal`: 33 new sources; per-source live counts match the per-publication counts measured in the spec (e.g. `44037985` → 3,585); a sample article with twins shows versions back to its 11.2 content.

### Task 6: Production phase 1 (owner OK required)

- [ ] **Step 1:** Confirm no active runs (`GET /api/extraction/runs?limit=20`), and that `prod.dump` from Task 5 is newer than the last completed run; otherwise take a fresh dump.
- [ ] **Step 2:** Copy the module into the worker: `kubectl cp backend/app/ops/netbackup_split.py <worker>:/tmp/netbackup_split.py -c worker`; dry run: `kubectl exec <worker> -c worker -- sh -c 'cd /app && PYTHONPATH=/app python /tmp/netbackup_split.py'` — expect the same counts as the rehearsal (± changes since the snapshot).
- [ ] **Step 3:** **Ask the owner for the go-ahead**, then rerun with `--apply`. Expect the `SplitResult` and exit 0.
- [ ] **Step 4:** Verify via API: 34 sources under the NetBackup product; original has 0 live; `GET /api/articles/delta` tail shows `updated` rows with new `source_id`s.

### Task 7: Production phase 2 + phase 3

- [ ] **Step 1:** Run the phase-2 runner (from this machine, against the API): `DOCEXTRACTOR_API_KEY=… .venv/bin/python -m app.ops.split_phase2`. Expect 33 completed runs, each ~all unchanged. On a stop: report the run's numbers and investigate before continuing (systematic-debugging).
- [ ] **Step 2:** **Ask the owner for the go-ahead** to delete the original source; then `DELETE /api/sources/cb3a6f6a-f51e-414e-b2ec-64eb9a2217b5`. Verify it emitted 0 `removed` rows (no live articles were left) and the 33 sources are intact.
- [ ] **Step 3:** Update memory (`cohesity-flex-single-page.md`) with the outcome, and clean up `/tmp` files in the worker and the scratchpad dump.
