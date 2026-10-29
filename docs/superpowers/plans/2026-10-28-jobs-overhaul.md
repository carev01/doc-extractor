# Jobs Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Jobs that cover sources by one tier plus explicit vendor/product/source picks, on "every N months / first-or-last weekday" schedules, with just-in-time auto-login, and a per-product Schedule view.

**Architecture:** Backend first: pure schedule rules (spec → cron, previews), the data model (tiers, job targets, new schedule fields), coverage resolution, the reworked fan-out with recorded skip reasons, JIT login in the worker, then the APIs and the Schedule aggregation. Frontend: tier admin + bulk tiering on the vendor list, a job editor built on `SourceTreePicker`, and a Schedule tab. A one-off ops script seeds Tier 1/2/3 and their jobs.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, Alembic, croniter 3.0.3, pytest-asyncio (async harness on `docextractor_test`); React 19 + TypeScript (`npm run build` = `tsc -b`, `npm run lint`).

**Spec:** `docs/superpowers/specs/2026-10-28-jobs-overhaul-design.md`

## Global Constraints

- `/api/jobs` and `/api/tiers` are admin-only (`require_admin` at router level); the Jobs page stays admin-only in the UI.
- A job has **at most one** tier. Tiers live on vendors only; a vendor has at most one tier.
- Schedule kinds: `daily`, `weekly`, `monthly_day`, `monthly_nth`. `interval_months ∈ {1, 2, 3, 4, 6, 12}` (must divide 12 so a fixed cron month list is periodic); `start_month` 1–12; `day_of_month` 1–28; `nth ∈ {first, last}`; `weekday` 0–6 with 0 = Sunday; `time_of_day` `HH:MM`; IANA `timezone`. `hourly` is removed.
- A refresh = a **completed `kind='extract'` run**, any trigger. `enrich` / `escalate` / `retry_blocked` never count. Completed with non-empty `blocked_pending` → warn, not ok.
- Never two active runs for a source: a job firing skips a source with a PENDING, RUNNING **or PAUSED** run (the DB index covers only PENDING/RUNNING, so PAUSED is checked explicitly).
- JIT login only for runs with `job_run_id IS NOT NULL`; at most one scripted login per realm per JobRun; never when `realm.status == LOGIN_FAILED`; horizon 1 hour.
- Skip reasons (exact strings): `active_run`, `paused_run`, `login_needed`, `login_failed`.
- Nullable JSONB columns use `JSONB(none_as_null=True)` (CLAUDE.md). New FKs to vendors/products/sources are `ON DELETE CASCADE` with `passive_deletes=True` on any new ORM relationship under them (CLAUDE.md).
- Queries over ~380 sources must not be N+1; lists that scale with data use `app.core.sql.any_of`.
- Commit footer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01MZDnbA8UDsbYyWSpfboJRk`.

## Review Focus

1. **Deleting a vendor, product or source that a job targets** → the target row disappears with it (FK cascade); the job keeps its other targets and its next firing doesn't error. → test in Task 3.
2. **A tier deleted while a job or vendor uses it** → refused with 409 naming the users, never a silent loss of coverage. → test in Task 6.
3. **`last` weekday in a month where it's the 5th occurrence vs the 4th** (e.g. last Saturday of Oct 2026 = 31st, of Apr 2027 = 24th) and the first weekday when the 1st is that weekday. → test in Task 1.
4. **A product with zero sources** in the Schedule view → shown as "no sources", neither fresh nor stale, never crashing the min/max aggregation. → test in Task 7.
5. **A JobRun whose children are all paused** → not stuck as an invisible RUNNING forever: its response exposes `children_paused` so the UI can flag it. → test in Task 4.

---

### Task 1: Schedule rules (pure)

**Files:**
- Modify: `backend/app/services/cron.py`
- Test: `backend/tests/test_schedule_rules.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class ScheduleSpec: kind: Literal["daily","weekly","monthly_day","monthly_nth"]; time_of_day: str; timezone: str = "UTC"; weekday: int | None = None; interval_months: int = 1; start_month: int | None = None; day_of_month: int | None = None; nth: Literal["first","last"] | None = None`
  - `def validate_spec(spec: ScheduleSpec) -> None` (raises `ValueError` with a human message)
  - `def spec_to_cron(spec: ScheduleSpec) -> str`
  - `def next_runs(spec: ScheduleSpec, after: datetime, count: int = 3) -> list[datetime]` (aware UTC), built on the existing `compute_next_run`.
- Months for N-monthly: `sorted({(start_month - 1 + k * N) % 12 + 1 for k in range(12 // N)})`. First weekday → `f"{w}#1"`, last → `f"L{w}"` in the dow field.

- [ ] **Step 1: Write failing tests**: `test_every_month_first_sunday` (spec monthly_nth, N=1, first, weekday 0, 02:00 UTC, after 2026-10-28 → 2026-11-01, 2026-12-06, 2027-01-03); `test_every_3_months_last_saturday_from_january` (→ 2027-01-30, 2027-04-24, 2027-07-31); `test_every_2_months_on_the_3rd_from_november` (→ 2026-11-03, 2027-01-03, 2027-03-03); `test_weekly_and_daily`; `test_timezone_is_respected` (Europe/Lisbon 02:00 → 02:00 local in UTC); `test_interval_must_divide_12` (5 → ValueError); `test_field_ranges` (day_of_month 29, weekday 7, missing nth → ValueError).
- [ ] **Step 2: Run** `cd backend && .venv/bin/python -m pytest tests/test_schedule_rules.py -q -p no:cacheprovider` — expect FAIL (ImportError).
- [ ] **Step 3: Implement** in `cron.py`; keep `build_cron`/`compute_next_run` until Task 2 removes the old frequency fields.
- [ ] **Step 4: Run** — expect PASS.
- [ ] **Step 5: Commit** `feat(jobs): every-N-months / first-last weekday schedule rules`.

### Task 2: Data model + migration

**Files:**
- Create: `backend/app/models/tier.py`, `backend/app/models/job_target.py`, `backend/alembic/versions/b1c2d3e4f5a6_jobs_overhaul.py` (down_revision `a7b8c9d0e1f2`)
- Modify: `backend/app/models/__init__.py`, `job.py`, `job_run.py`, `extraction_run.py`, `vendor.py`, `source.py`, `backend/app/schemas/job.py`, `backend/app/routes/jobs.py` (compile against the new model; behaviour in Task 6)
- Test: `backend/tests/test_jobs_model.py`

**Interfaces:**
- Produces:
  - `Tier(id, name: str unique, sort_order: int)`; `Vendor.tier_id: UUID | None` FK `tiers.id ON DELETE RESTRICT`.
  - `JobTarget(id, job_id FK jobs CASCADE, vendor_id | product_id | source_id — exactly one non-null, each FK CASCADE)`, CHECK constraint `num_nonnulls(vendor_id, product_id, source_id) = 1`, unique per (job_id, vendor_id), (job_id, product_id), (job_id, source_id).
  - `Job`: add `tier_id` (FK tiers RESTRICT, nullable), `schedule_kind`, `interval_months`, `start_month`, `nth`, `weekday`; drop `frequency`, `day_of_week`; keep `time_of_day`, `day_of_month`, `timezone`, `cron`, `next_run_at`, `last_run_at`. Relationship `targets` (cascade on the ORM side is fine — CASCADE FK + `passive_deletes=True`).
  - `JobRun.skipped: list[dict] | None` = `JSONB(none_as_null=True)` — items `{"source_id": str, "reason": str}`.
  - `ExtractionRun.auto_relogin_used: bool` default False, server_default false.
  - Drop `DocumentationSource.job_id` (+ relationship) — 0 rows assigned in production.
  - `def job_spec(job: Job) -> ScheduleSpec` in `cron.py`.

- [ ] **Step 1: Write failing tests**: `test_target_requires_exactly_one_ref` (IntegrityError on two refs / none); `test_deleting_a_product_drops_its_target_only` (job keeps its vendor target); `test_tier_in_use_cannot_be_deleted_at_db_level` (RESTRICT); `test_job_spec_roundtrip` (`job_spec(job)` → `spec_to_cron` equals `job.cron` after save via the route helper).
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement** models + migration (upgrade: create tables/columns, drop `documentation_sources.job_id` and `jobs.frequency/day_of_week`; downgrade reverses). Update `schemas/job.py` to the new fields (`JobScheduleConfig` becomes `ScheduleSpec` fields + `enabled`, `tier_id`), and make `routes/jobs.py` compile (assignment endpoints removed in Task 6). Remove the per-source job assignment code in `SourceList.tsx` later (Task 10).
- [ ] **Step 4: Run** the new tests and `tests/test_jobs*.py tests/test_scheduler*.py` — fix every test that pinned `frequency`/`job_id` (update, don't delete, unless the behaviour is gone).
- [ ] **Step 5: Exercise the migration** on a scratch DB with **both** URLs set (`DOCEXTRACTOR_DATABASE_URL` and `_SYNC` → `docextractor_mig`), `alembic current` first; upgrade → downgrade → upgrade.
- [ ] **Step 6: Commit** `feat(jobs): tiers, job targets and new schedule fields`.

### Task 3: Coverage resolution

**Files:**
- Create: `backend/app/services/job_coverage.py`
- Test: `backend/tests/test_job_coverage.py`

**Interfaces:**
- Consumes: models from Task 2.
- Produces:
  - `async def resolve_sources(db, job_ids: Sequence[UUID]) -> dict[UUID, set[UUID]]` — job → covered source ids = tier vendors' sources ∪ vendor targets' sources ∪ product targets' sources ∪ source targets. One query per target kind, not per job.
  - `async def normalize_targets(db, source_ids: Sequence[UUID]) -> Targets` with `@dataclass class Targets: vendor_ids: list[UUID]; product_ids: list[UUID]; source_ids: list[UUID]` — a product all of whose current sources are selected becomes a product target; a vendor all of whose products are fully selected becomes a vendor target; the rest stay sources.

- [ ] **Step 1: Write failing tests**: `test_tier_is_dynamic` (tier a second vendor after creating the job → its sources resolve); `test_vendor_and_product_targets_include_later_additions`; `test_union_has_no_duplicates`; `test_normalize_full_product_and_full_vendor`; `test_normalize_partial_product_keeps_sources`; `test_product_with_no_sources_is_never_a_target`; Review Focus 1: `test_deleted_target_disappears_and_resolution_still_works`.
- [ ] **Step 2–4:** run (FAIL) → implement → run (PASS).
- [ ] **Step 5: Commit** `feat(jobs): resolve and normalize job coverage`.

### Task 4: Fan-out with recorded skips; rollup with paused children

**Files:**
- Modify: `backend/app/services/scheduling.py`
- Test: `backend/tests/test_job_fanout.py`

**Interfaces:**
- Consumes: `resolve_sources` (Task 3); `bulk_extract.realm_blocks_extraction` and `ACTIVE_STATUSES`.
- Produces: `fan_out_job(db, job, trigger, now=None) -> JobRun | None` (same signature) now: resolves coverage; for each source, skip with reason —
  - `paused_run` if a PAUSED run exists;
  - `active_run` on the IntegrityError coalesce (existing savepoint path);
  - `login_failed` if its realm is `LOGIN_FAILED`;
  - `login_needed` if `realm_blocks_extraction(realm)` and the realm has no saved credentials;
  - (realms **with** credentials are enqueued — the run logs in, Task 5);
  - writes `job_run.skipped`; `sources_total` = enqueued.
  - `reconcile_job_runs` unchanged in status semantics; `JobRunResponse` gains `children_paused: int` and `skipped: list[{source_id, reason}]` (computed in the route, Task 6).

- [ ] **Step 1: Write failing tests**: one per skip reason (`test_paused_run_is_skipped_not_doubled`, `test_active_run_coalesces`, `test_login_failed_realm_skipped`, `test_cookie_realm_expired_skipped_as_login_needed`, `test_credential_realm_expired_is_enqueued`), `test_job_with_no_coverage_returns_none`, Review Focus 5: `test_all_children_paused_jobrun_reports_children_paused`.
- [ ] **Step 2–4:** run (FAIL) → implement → run (PASS); also run `tests/test_scheduler*.py`.
- [ ] **Step 5: Commit** `feat(jobs): fan out over resolved coverage and record skip reasons`.

### Task 5: Just-in-time login for job runs

**Files:**
- Modify: `backend/app/services/auth/realm_manager.py`, `backend/app/services/firecrawl.py` (the three `ensure_session` calls ~L2234/2410/2535 and the `NeedsLoginError` pause branch ~L2912)
- Test: `backend/tests/test_job_jit_login.py`

**Interfaces:**
- Produces: `async def session_for_run(db, realm, *, run: ExtractionRun, horizon: timedelta = timedelta(hours=1)) -> dict` in `realm_manager`:
  - if `run.job_run_id` and the realm has `username`+`password` and `realm.status != LOGIN_FAILED` and (status ≠ ACTIVE, or no snapshot, or `session_expires_at(realm)` ≤ now + horizon) and the realm's `last_login_at` is older than the run's JobRun `created_at` → `run_scripted_login`;
  - then return `ensure_session(db, realm)`.
- Mid-run: in the `NeedsLoginError` branch, if `run.job_run_id` and the realm has credentials and not `run.auto_relogin_used` → `run_scripted_login`; on success set `run.auto_relogin_used = True`, `run.status = PENDING` (resume from checkpoint, exactly what `/resume` does) instead of PAUSED; on failure fall through to today's PAUSE path.

- [ ] **Step 1: Write failing tests** (monkeypatch `realm_manager.run_scripted_login` with a counting fake; drive `session_for_run` directly and the pause branch through the smallest seam available): `test_manual_run_never_logs_in`; `test_job_run_logs_in_when_expiring_within_an_hour`; `test_login_failed_realm_is_not_retried`; `test_one_login_per_realm_per_jobrun` (two runs of one JobRun → one login); `test_mid_run_auth_wall_relogs_once_then_pauses`.
- [ ] **Step 2–4:** run (FAIL) → implement → run (PASS); also run `tests/test_*auth*.py tests/test_browserless_auth.py`.
- [ ] **Step 5: Commit** `feat(jobs): just-in-time scripted login for job-triggered runs`.

### Task 6: Tiers and jobs API

**Files:**
- Create: `backend/app/routes/tiers.py`, `backend/app/schemas/tier.py`
- Modify: `backend/app/routes/jobs.py`, `backend/app/schemas/job.py`, `backend/app/main.py` (register router), `backend/app/routes/vendors.py` (`tier_id` in responses)
- Test: `backend/tests/test_tiers_api.py`, `backend/tests/test_jobs_api.py`

**Interfaces:**
- Produces (all admin-only):
  - `GET/POST /api/tiers`, `PATCH/DELETE /api/tiers/{id}` (DELETE → 409 `{"detail": "...", "vendors": n, "jobs": n}` when in use).
  - `PUT /api/tiers/{id}/vendors` `{vendor_ids}` and `PUT /api/tiers/none/vendors` to clear — bulk set tier.
  - `GET /api/jobs` / `GET /api/jobs/{id}`: `JobResponse` with schedule fields, `tier_id`, `targets: {vendor_ids, product_ids, source_ids}`, `coverage: {sources: n, products: n, overlap_sources: n, login_sources: n}`, `next_runs: [3 datetimes]`.
  - `POST /api/jobs`, `PATCH /api/jobs/{id}`: schedule fields validated by `validate_spec`; `cron`/`next_run_at` derived.
  - `PUT /api/jobs/{id}/coverage` `{source_ids}` → `normalize_targets` replaces the job's targets.
  - `POST /api/jobs/schedule/preview` `{ScheduleSpec fields}` → `{next_runs: [3]}` or 422 with the validation message.
  - `GET /api/jobs/catalog` → `[{vendor_id, vendor_name, tier_id, products: [{product_id, product_name, sources: [{source_id, source_name, auth_realm: str | null}]}]}]`.
  - `POST /api/jobs/{id}/run` unchanged; old per-source assign endpoints removed.

- [ ] **Step 1: Write failing tests** covering each endpoint's happy path, validation (interval 5 → 422 message), admin-only (read_write principal → 403), Review Focus 2 `test_deleting_a_tier_in_use_is_refused`, bulk set/clear tier, coverage normalization via the API, `coverage.overlap_sources` when two jobs share a source.
- [ ] **Step 2–4:** run (FAIL) → implement → run (PASS).
- [ ] **Step 5: Commit** `feat(jobs): tiers and job coverage/schedule API`.

### Task 7: Schedule view aggregation

**Files:**
- Create: `backend/app/services/schedule_view.py`
- Modify: `backend/app/routes/jobs.py` (`GET /api/jobs/schedule`)
- Test: `backend/tests/test_schedule_view.py`

**Interfaces:**
- Produces: `GET /api/jobs/schedule` → `{products: [{product_id, product_name, vendor_name, tier_name | null, jobs: [names], source_count, last_ok_at | null, latest: {at, outcome: "ok"|"warn"|"failed"|"skipped", trigger, job_name | null, updated: int, reason | null} | null, next_at | null, scheduled_sources: int, sources: [{source_id, source_name, last_ok_at, latest, next_at, jobs}]}]}`.
- Per source: `last_ok_at` = latest completed extract run with empty `blocked_pending`; `latest` = most recent of (terminal extract run, JobRun skip entry); `next_at` = min `next_run_at` of **enabled** covering jobs. Per product: `last_ok_at` = **min** over sources (null if any source never ok); `latest` = most recent among sources; `next_at` = **max** over scheduled sources; `scheduled_sources` < `source_count` ⇒ "partly scheduled".

- [ ] **Step 1: Write failing tests**: `test_least_fresh_source_drives_product_last_ok`; `test_enrich_runs_do_not_count`; `test_blocked_pages_make_it_warn`; `test_skip_reason_shows_as_latest`; `test_next_is_latest_among_sources_and_partly_scheduled_flag`; `test_disabled_job_does_not_schedule`; Review Focus 4 `test_product_with_no_sources`; `test_query_count_is_constant` (count statements via a `before_cursor_execute` listener for 3 vs 30 products — same number).
- [ ] **Step 2–4:** run (FAIL) → implement → run (PASS).
- [ ] **Step 5: Commit** `feat(jobs): per-product schedule view endpoint`.

### Task 8: Seed script

**Files:**
- Create: `backend/app/ops/seed_jobs.py`
- Test: `backend/tests/test_seed_jobs.py`

**Interfaces:**
- Produces: `async def seed(db) -> dict` (idempotent; never commits) + `python -m app.ops.seed_jobs [--apply]` (dry run = apply + rollback, as `netbackup_split`): creates tiers `Tier 1/2/3` (sort 1–3) if absent; jobs `Tier 1` (monthly_nth, N=1, first, Sunday), `Tier 2` (N=2, first, Saturday), `Tier 3` (N=3, last, Saturday), all 02:00 UTC, `enabled=False`, `start_month` = next month, each `tier_id` = its tier; deletes a job named `Major competitors` only if it has no targets and no JobRuns.

- [ ] **Step 1–4:** tests `test_seed_creates_three_disabled_staggered_jobs`, `test_seed_is_idempotent`, `test_major_competitors_deleted_only_when_unused` → FAIL → implement → PASS.
- [ ] **Step 5:** full backend suite `pytest -q -p no:cacheprovider` → exit 0. **Commit** `feat(ops): seed tiers and tier jobs`.

### Task 9: Frontend — tiers

**Files:**
- Modify: `frontend/src/types/index.ts`, `frontend/src/api/client.ts`, `frontend/src/components/VendorList.tsx`
- Create: `frontend/src/components/TierManager.tsx`

**Interfaces:**
- Produces: types `Tier`, client `listTiers/createTier/updateTier/deleteTier/setVendorsTier(tierId | null, vendorIds)`. Vendor list (admin only): a tier badge per vendor, row checkboxes, and a "Set tier ▾" action for the checked vendors. `TierManager`: create / rename / reorder / delete (shows the 409 counts).

- [ ] **Step 1:** implement; **Step 2:** `npm run build && npm run lint` → clean; **Step 3: Commit** `feat(ui): tiers and bulk vendor tiering`.

### Task 10: Frontend — job editor

**Files:**
- Modify: `frontend/src/components/SourceTreePicker.tsx` (optional vendor grouping: `vendors?: {id, name, products: TreeProduct[]}[]` with a tri-state vendor row, collapsed by default), `frontend/src/components/JobsManager.tsx` (rewrite as list + editor), `frontend/src/components/SourceList.tsx` (remove the per-source job dropdown), `frontend/src/types/index.ts`, `frontend/src/api/client.ts`
- Delete: `frontend/src/components/SourcePicker.tsx`

**Interfaces:**
- Consumes: Task 6 endpoints.
- Produces: the editor — name, enabled, tier select, coverage picker (catalog → tree; initial selection = sources resolved from `targets`; save → `PUT /coverage` with ticked source ids), schedule form (kind; N select from {1,2,3,4,6,12}; start month; day / first-last + weekday; time; timezone) with a live "Next runs" list from `/schedule/preview`, and the coverage summary line from `coverage`.

- [ ] **Step 1:** implement; **Step 2:** `npm run build && npm run lint` → clean; **Step 3: Commit** `feat(ui): job editor with tier, tree coverage and schedule preview`.

### Task 11: Frontend — Schedule tab

**Files:**
- Create: `frontend/src/components/ScheduleView.tsx`
- Modify: `frontend/src/components/JobsView.tsx` (new first tab "Schedule"), types/client

**Interfaces:**
- Consumes: `GET /api/jobs/schedule`.
- Produces: layout B — rows per product (vendor › product, tier, jobs), Last refreshed (date, ✓/⚠ outcome, trigger, "last OK" when the latest isn't ok), Next ("in N days", "partly scheduled k/n", "not scheduled"), text filter + tier filter, expandable source rows.

- [ ] **Step 1:** implement; **Step 2:** `npm run build && npm run lint` → clean; **Step 3: Commit** `feat(ui): Schedule tab — per-product last/next refresh`.

### Task 12: Docs, deploy, seed (owner OK per step)

- [ ] **Step 1:** `docs/API.md` — Tiers and new Jobs sections; `CLAUDE.md` — one invariant: job coverage = ≤1 tier + targets, resolved at firing time; PAUSED is checked explicitly at fan-out; JIT login only for job runs, never retried after LOGIN_FAILED. Commit `docs: jobs overhaul`.
- [ ] **Step 2:** Open PR; after CI green and **owner OK**, merge and deploy (Helm, both image tags). Confirm `alembic current` = the new head and no runs were active at deploy.
- [ ] **Step 3:** `kubectl exec deploy/docextractor-worker -c worker -- python -m app.ops.seed_jobs` (dry run) → expected 3 tiers / 3 jobs / Major competitors deleted; then `--apply`.
- [ ] **Step 4:** Report to the owner: jobs are disabled until vendors are tiered; how to bulk-tier and enable.
