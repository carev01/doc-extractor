# Jobs overhaul — design

**Date:** 2026-10-28 · **Status:** reviewed; owner answers folded in

## Why

Keeping ~380 sources fresh today means clicking Extract per source (bulk extract, #234, now helps but is still manual). Jobs exist but are effectively unused: one disabled job with no sources in production, and 0 of 383 sources assigned. The owner finds them unfriendly — sources are assigned one at a time, the schedule options don't fit how they think about refresh cadence, and there's no view of when things ran or will run.

How the owner wants to run it: vendors grouped by importance into tiers — **Tier 1 monthly, Tier 2 every two months, Tier 3 quarterly** — plus the ability to put specific vendors/products/sources on their own cadence. And one view that answers, per product, *"when was this refreshed last, and when is it refreshing again?"*

## Goals

1. Jobs cover sources by **tier** (dynamic: a vendor tiered later is picked up automatically) **plus explicit picks** of vendors, products or sources.
2. Schedules expressed the way the owner thinks: **every N months** on a fixed day, the **first** or **last** given weekday; plus weekly and daily.
3. Jobs reach login-walled vendors without manual steps where a login can be automated.
4. A **Schedule** view: one row per product with last refresh (outcome, trigger), next scheduled refresh, and gaps ("not scheduled").
5. A job editor that makes coverage and schedule obvious before saving.

## Non-goals

- **General tags** (free-form labels for filtering views). Agreed as a separate, later feature; nothing here depends on it.
- **Excludes** in job coverage (keeping one source of a tiered vendor out). Not requested; revisit if needed.
- Changing **manual / bulk** extract behaviour, including how they treat expired logins.
- Notifications on job completion (webhooks/Slack already exist; not part of this).
- Per-vendor (non-admin) access to jobs: `/api/jobs` is admin-only at router level today and stays so; the Schedule view lives in the admin-only Jobs page (CLAUDE.md: a job spans vendors, so per-vendor scoping is a redesign, not a filter).

## Anti-goals

- A product looking **fresher than it is** in the Schedule view.
- A job firing that silently does nothing for a source (e.g. skipped for a login) with no trace in the view.
- Two active runs for one source because two jobs fired (the existing never-two-active-runs rule must hold).
- Parallel scrape load: job runs go through the ordinary queue, one at a time.

## Decisions (owner's, as agreed)

### Tiers
- A **managed list** of tiers (name + display order), e.g. Tier 1 / Tier 2 / Tier 3 — not free text, so names can't drift.
- A **vendor has at most one tier**. Tiers live on vendors only; a product that needs a different cadence is added explicitly to a faster job.
- Tiers are for job selection only.

### Job coverage
A job covers the union of:
- **at most one tier** (owner: "keep jobs limited to one Tier selection") — every source of every vendor holding that tier, evaluated when the job fires;
- **explicit picks**: whole vendors, whole products (both dynamic — new products/sources under them are included), and individual sources.

Coverage is chosen with the vendor → product → source tree picker from bulk extract (`SourceTreePicker`), extended so that a fully ticked vendor/product is stored as a *vendor/product target* (dynamic), while a partly ticked one stores its ticked *sources*.

This replaces today's "a source belongs to at most one job" (`documentation_sources.job_id`). In production no source is assigned, so there is nothing to migrate beyond the schema.

### Overlap
A source may be covered by several jobs (e.g. Tier 1 monthly + a weekly job naming one product). Allowed and harmless:
- it refreshes on each schedule;
- if a run for the source is already queued or running when a job fires, the source is skipped for that firing (existing coalescing in `fan_out_job` / the active-run constraint);
- the job editor and the Schedule view show "also covered by …".

### Logins
- Realms **with saved credentials** (today: Cohesity Docs, Securiti, AvePoint Learn — the owner believed only AvePoint works this way) are logged in **automatically, just in time**: when a *job-triggered* run starts and its source's realm session is expired, the worker runs the realm's scripted login (`realm_manager.run_scripted_login`) first, then extracts. At most one login per realm per job firing.
- If that login fails — or the realm has no saved credentials (cookie-upload realms: Rubrik Docs, Eon Docs) and the session is expired — the source is **skipped with a reason** ("login failed" / "login needed"), recorded so the Schedule view shows it.
- "Just in time" matters because a firing enqueues everything at once and the queue runs one source at a time; a login done at fan-out could expire before a late source starts.

### Schedules
Forms, each at a time of day in a timezone:
- **Every N months** (N ≥ 1) on: **day D** of the month (1–28), or the **first <weekday>**, or the **last <weekday>**.
- **Weekly** on a weekday.
- **Daily**.

`hourly` is dropped (nothing needs it; the one existing job has no frequency set).

- **Starting month** (designer's call, not objected): an every-N-months schedule has a starting month (default: next month), so "every 3 months starting January" runs Jan/Apr/Jul/Oct.
- **Implementation is cron-expressible, verified:** a month list for N-monthly from the start month, `dow#1` for first weekday, `L<dow>` for last weekday (`croniter 3.0.3`: `0 2 * 1,4,7,10 L6` → last Saturdays Oct 31 / Jan 30 / Apr 24 — correct). The existing `cron` + `compute_next_run` mechanism stays; the friendly fields are the source of truth and the cron string is derived from them.
- The job editor previews the **next three run dates** as you edit.

### Schedule view (chosen layout B: product timeline)
A table, one row per product:
- **Product**: vendor › product, its tier, and the jobs covering it ("Tier 1 + Weekly").
- **Last refreshed**: date, outcome (✓ refreshed — N updated / unchanged; ⚠ skipped or failed, with reason) and trigger (job name, manual, bulk). Counts **any** extraction, not only job runs. When the latest attempt was skipped or failed, also **last OK** date.
- **Next**: the soonest next run among covering jobs, with "in N days"; **not scheduled** when no enabled job covers it.
- **Multi-source products**: "last refreshed" is the product's **least recently refreshed source** (so it never looks fresher than it is); expanding a row lists its sources with their own last/next.
- Filters: vendor/product text, tier.

### Job editor
Name · enabled · tier (single select, optional) · explicit picks (tree picker) · schedule form · next three runs · a coverage summary ("covers 14 products / 62 sources; 2 also covered by Weekly; 3 need a login").

### Tier management
Assign a vendor's tier on the vendor (single select). Create / rename / reorder / delete tiers in a small admin-only list. Deleting a tier in use is refused (or clears it from vendors — builder's call, but never silently drops a job's tier coverage without saying so).

### Paused runs (designer's call, from review)
Today a session expiring mid-run **pauses** the run (checkpoint kept, waits for Resume), `uq_active_run_per_source` covers only PENDING/RUNNING, and `reconcile_job_runs` treats only COMPLETED/FAILED/CANCELLED as finished — so a paused child leaves its JobRun RUNNING forever and a firing could create a second run beside a paused one.
- A firing **skips** a source that has a PAUSED run, with reason "paused run waiting" — never a second run beside it (PAUSED counts as active, as `bulk_extract.ACTIVE_STATUSES` and `delta_feed._ACTIVE` already treat it).
- A **job-triggered** run that hits an expired session mid-run on a realm **with saved credentials** logs in again (same rules as below) and continues; otherwise it pauses as today.
- For the JobRun rollup a paused child is **not finished**; the JobRun shows it as needing attention rather than hanging silently. A paused run is **not a refresh** for the Schedule view.

### What counts as a refresh (designer's call, from review)
Only **completed `extract`-kind runs**, from any trigger (`scheduled`, `manual`, `force`, `bulk`, `version-bump`). `enrich`, `escalate` and `retry_blocked` runs don't re-check the vendor and never count. A completed run that still has `blocked_pending` pages is **⚠ with the count**, not ✓.

### Auto-login trigger and lockout safety (designer's call, from review)
- Log in when `session_expired(realm)` (cookie expiry), the realm status is not ACTIVE, **or** the session expires within the next hour (large sources run for hours). Note `realm_manager.ensure_session` alone ignores cookie expiry, so the trigger is defined here, not inherited.
- At most **one attempt per realm per firing**. After a failed scripted login the realm stays LOGIN_FAILED and **later firings don't retry** until the credentials change or a manual Login/Test succeeds — repeated failed logins could lock the Cohesity / Securiti / AvePoint accounts. Those sources are skipped with "login failed".

### Coverage contents (designer's call, from review)
A tier or vendor/product target covers **every** source under it, including PDF sources and sources never extracted (their first run is a first extraction). Sources that fail show ⚠ in the view — no new per-source flag (excludes stay a non-goal). Versioned products hold only their current-version sources, so no version filtering is needed.

### "Next" for a partly covered product (designer's call, from review)
Same never-fresher rule as "last refreshed": a product's **Next** is the *latest* next run among its sources, and a product with any uncovered source is flagged **partly scheduled (k of n)**.

### First setup (owner, from review)
- **Bulk tier assignment**: tier many vendors at once — e.g. a tier column with checkboxes and "set tier" on the vendor list — instead of one select per vendor.
- **Seed** tiers *Tier 1 / Tier 2 / Tier 3* and three jobs, created **disabled** (nothing runs until the owner has tiered vendors and enables them) and **staggered** so they never fire the same night (designer's call, owner to adjust): Tier 1 — every month, first Sunday; Tier 2 — every 2 months, first Saturday; Tier 3 — every 3 months, last Saturday; all 02:00, each job covering its tier.
- **Delete** the unused "Major competitors" job (disabled, no sources).

## Current state the builder needs (verified)

- `app/models/job.py` (`Job`: `enabled`, `frequency` hourly|daily|weekly|monthly, `time_of_day`, `day_of_week`, `day_of_month` 1–28, `cron`, `timezone`, `next_run_at`, `last_run_at`), `app/models/job_run.py` (`JobRun` groups the child `ExtractionRun`s, status rolled up by the scheduler tick).
- `app/services/scheduling.py`: `fan_out_job` enqueues one run per source with `job_run_id`, coalescing sources with an active run via a savepoint; `reconcile_job_runs` rolls up statuses. `app/services/cron.py`: `compute_next_run(cron, timezone, after)` via croniter.
- `app/routes/jobs.py`: CRUD, `PUT /{job_id}/sources`, per-source assign/unassign, `POST /{job_id}/run`, runs listing. Admin-only router.
- Frontend: `JobsView.tsx` (tabs: Active / Recent / Job runs / Exports + jobs management via `JobsManager.tsx`), `SourcePicker.tsx` (flat, job-specific — to be replaced by `SourceTreePicker`), per-source job dropdown in `SourceList.tsx` (goes away with one-job-per-source).
- Auth realms: `app/routes/auth_realms.py` `POST /{realm_id}/login` → `realm_manager.run_scripted_login(db, realm)`, requires `username` + `password`; `session_expired(realm)` / `RealmStatus.EXPIRED` as used by the trigger route and `bulk_extract.realm_blocks_extraction`.
- Production: 1 job (disabled, no schedule, no sources); 0/383 sources assigned; 5 realms (3 with saved credentials).

## Left to the builder

- Schema shape (e.g. `tiers`, `vendors.tier_id` FK `ON DELETE SET NULL` or restrict, a `job_targets` table of `(job_id, kind ∈ {vendor, product, source}, ref_id)` with `ON DELETE CASCADE` per target kind), migrating away from `documentation_sources.job_id`, and the friendly schedule columns.
- How "last refreshed / next" are computed for the Schedule view (one aggregated endpoint is expected; ~380 sources, so a single query or a small number of them — not N+1).
- Where skip reasons for job-triggered sources are recorded (a cancelled/skipped child run with a reason is one option) so the view can show them.
- Tier deletion semantics (see above), UI layout details, and whether the Schedule view becomes the Jobs page's default tab.
- Keeping `access.ts` in step if any route permission changes (none planned).
