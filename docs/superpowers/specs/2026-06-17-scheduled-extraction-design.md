# Scheduled Extraction — Design

**Date:** 2026-06-17
**Status:** Approved (design); pending implementation plan
**Goal addressed:** "It must provide a UI … to schedule recurrent runs" + "offer efficient incremental runs to capture changes over time" (CLAUDE.md project goals).

## Summary

Add unattended, recurring extraction to DocExtractor. Today extractions only ever run when a user clicks "extract," and they run **in-process** in the API via FastAPI `BackgroundTasks`. The final deployment target is Kubernetes, so this work decomposes the monolith into three cooperating processes coordinated entirely through Postgres:

- **web** — serves the API/UI and the Firecrawl webhook receiver; *enqueues* runs, never executes them.
- **worker** — claims pending runs from a Postgres-backed queue and executes the existing extraction engine.
- **scheduler** — a single replica that ticks periodically to enqueue due schedules and reap dead runs.

Scheduling is then just one producer into the job queue. Manual triggers become the other producer; both share the same execution path.

This round delivers **app-level decomposition only**: three runnable entrypoints from one image, wired into `docker-compose` as three services. No Kubernetes manifests are authored here, but every process maps 1:1 to a future Deployment.

## Decisions (locked during brainstorming)

| Decision | Choice | Rationale |
|---|---|---|
| Execution model | Scheduler + worker + Postgres job queue | Survives pod death; scales extraction independently of the API; correct for K8s. |
| Deliverable scope | App-level decomposition only (no manifests yet) | Proves the architecture end-to-end under compose; K8s-shaped. |
| Cadence config | Friendly presets, stored as cron + timezone | Friendly UX, fully general engine, raw-cron addable later without migration. |
| Overlap policy | Skip / coalesce — one active run per source | Avoids duplicate Firecrawl work; matches existing 409-on-active behavior. |
| Missed-run policy | Catch up once, then resume | Keeps content fresh without stacking; falls out of `next_run_at <= now`. |

## Architecture & process topology

Three processes, **one image**, selected by command:

| Process | Command | Replicas | Role |
|---|---|---|---|
| web | `uvicorn app.main:app` | N | API/UI + Firecrawl `/api/extraction/webhook/{run_id}` receiver. Enqueues runs. |
| worker | `python -m app.worker` | N | Claims pending runs, executes `extract_source`. |
| scheduler | `python -m app.scheduler` | 1 | Ticks ~30s: enqueues due schedules, reaps dead runs. |

Coordination is **100% through Postgres** — no process holds shared in-memory state, so any pod can die and be replaced.

### Firecrawl webhook stays on the web process (load-bearing detail)

`extract_source` submits a batch to Firecrawl with a webhook URL; Firecrawl POSTs each scraped page back. In the split model:

- The **worker** runs `extract_source` end to end: TOC discovery → submit batch → poll for completion → finalize (removed-page detection).
- Firecrawl's per-page webhooks keep hitting the **web** service at the stable `WEBHOOK_BASE_URL`, which writes pages to the DB exactly as today.
- Worker and webhook already coordinate purely via DB + the Firecrawl job id — no in-memory handoff — so the split needs **no change to the webhook logic**, and **workers need no inbound network/ingress** (only outbound calls to Firecrawl + DB).

### Migrations / startup

The **web** entrypoint keeps running `alembic upgrade head` before serving (as the Dockerfile does today). Worker and scheduler **do not** migrate; they retry-connect until the schema exists, avoiding three processes racing Alembic. In K8s this later becomes an initContainer/Job (out of scope here, noted).

`docker-compose.yml` gains `worker` and `scheduler` services built from the same `./backend` context with identical env, differing only in `command:`.

## Data model & queue semantics

**The `ExtractionRun` table *is* the job queue.** No separate queue table — a run row's lifecycle is its queue state. This keeps one unified ledger and reuses the existing polling/status endpoints.

### New table: `schedules` (one schedule per source)

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `source_id` | uuid FK → sources, **unique** | one schedule per source |
| `enabled` | bool, default false | the Off toggle |
| `cron` | text | e.g. `0 2 * * *` (preset-generated) |
| `timezone` | text, default `UTC` | IANA name, e.g. `Europe/Lisbon` |
| `next_run_at` | timestamptz, nullable | computed from cron+tz; the tick key |
| `last_run_at` | timestamptz, nullable | |
| `last_run_id` | uuid FK → extraction_runs, nullable | links to the run it kicked off |
| `created_at` / `updated_at` | timestamptz | |

### `extraction_runs` gains queue columns

| Column | Type | Notes |
|---|---|---|
| `status` | extend enum | add **`pending`** and `cancelled` → `pending\|running\|completed\|failed\|cancelled` |
| `trigger` | text | `manual` \| `scheduled` — provenance |
| `claimed_by` | text, nullable | worker identity (pod/hostname) |
| `claimed_at` | timestamptz, nullable | when claimed |
| `heartbeat_at` | timestamptz, nullable | worker bumps periodically; drives the reaper |
| `attempts` | int, default 0 | incremented on claim; bounds retries |

### Indexes — these carry the concurrency guarantees

1. **Queue scan:** `ix_runs_pending ON extraction_runs (created_at) WHERE status = 'pending'` — cheap FIFO claim target.
2. **One active run per source (the linchpin):**
   ```sql
   CREATE UNIQUE INDEX uq_active_run_per_source
     ON extraction_runs (source_id)
     WHERE status IN ('pending', 'running');
   ```
   Makes "skip/coalesce" a **database invariant**, not application luck. Both the scheduler and the manual-trigger endpoint attempt an insert; if a run is already active for that source the insert raises a unique-violation, which the caller catches and treats as "already active → skip (scheduler) / 409 (manual)." No race window.

### Lifecycle

`pending` (enqueued) → `running` (claimed) → `completed` / `failed`. The reaper can return a stale `running` to `pending` within the same row (the partial unique index stays satisfied). `ArticleVersion.extraction_run_id`, changelog, and browse logic are unaffected; they only gain `trigger` provenance.

### Migration

One Alembic revision adds the table, the columns, the enum value, and both indexes. Existing rows backfill `trigger='manual'`, `attempts=0`. No content/version data is migrated.

## Worker behavior (`python -m app.worker`, N replicas)

Async claim loop:

```
loop forever:
  run = claim_one_pending_run()         # atomic; see below
  if not run:
      sleep(POLL_INTERVAL ~2s); continue
  start heartbeat task (bump heartbeat_at every ~15s)
  try:
      extract_source(db, run.source_id, run_id=run.id)   # existing engine, unchanged
      mark run completed
  except Exception:
      mark run failed (or requeue under retry cap)
  stop heartbeat
```

**Atomic claim** — `FOR UPDATE SKIP LOCKED` lets N workers pull concurrently without grabbing the same row:

```sql
WITH next AS (
  SELECT id FROM extraction_runs
  WHERE status = 'pending'
  ORDER BY created_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
UPDATE extraction_runs r
SET status='running', claimed_by=:worker_id, claimed_at=now(),
    heartbeat_at=now(), attempts = attempts + 1, started_at = now()
FROM next WHERE r.id = next.id
RETURNING r.*;
```

The worker owns the `source.status = EXTRACTING` transition (the route no longer sets it). **Worker identity** = pod hostname / `HOSTNAME`, stored in `claimed_by` for observability.

## Reaper & retries (run inside the scheduler tick)

A worker that dies mid-run leaves a row stuck `running`. The reaper requeues it:

```sql
UPDATE extraction_runs
SET status = CASE WHEN attempts >= :max_attempts THEN 'failed' ELSE 'pending' END,
    claimed_by=NULL, claimed_at=NULL,
    error_message = COALESCE(error_message, 'worker lost')
WHERE status='running' AND heartbeat_at < now() - :stale_interval;   -- e.g. 5 min
```

- `extract_source` is **re-runnable from scratch** (rebuilds TOC, upserts articles by `source_url`), so requeuing a partial run is safe.
- `attempts` caps retries (default `max_attempts=3`); then `failed`. The partial unique index is satisfied throughout.
- `stale_interval` is generous (5 min) so a long-but-healthy extraction with live heartbeats is never reaped.

## Scheduler behavior (`python -m app.scheduler`, 1 replica)

Tick every ~30s, wrapped in `pg_try_advisory_lock(<const>)` as cheap insurance against an accidental second replica:

1. **Reap** stale runs (above).
2. **Enqueue due schedules:**
   ```sql
   SELECT * FROM schedules WHERE enabled AND next_run_at <= now();
   ```
   For each:
   - Attempt to insert a `pending` ExtractionRun (`trigger='scheduled'`).
     - Success → set `last_run_at`, `last_run_id`.
     - Unique-violation (source already active) → **coalesce**: log "skipped, already active," leave it.
   - **Recompute `next_run_at`** = `croniter(cron, now, tz).get_next()`. Computing relative to **now** is exactly what yields "catch up once, then resume": if the scheduler was down past a fire time, `next_run_at <= now` is true on recovery → one enqueue → next fire recomputed forward; never stacks.

**Cron engine:** `croniter` (new dependency) + stdlib `zoneinfo`. `next_run_at` always stored in UTC; cron evaluated in the schedule's timezone.

**On create/edit/enable** (web API), `next_run_at` is computed immediately so the first fire doesn't wait for an edit-time tick. Disabling sets `enabled=false` (the row's `next_run_at` is ignored while disabled).

## API & frontend

### Backend API — schedule management (sources router)

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/api/sources/{id}/schedule` | Returns config + `enabled`, `cron`, `timezone`, `next_run_at`, `last_run_at`, thin `last_run` summary. 404 if none. |
| `PUT` | `/api/sources/{id}/schedule` | Upsert. Body: `{ enabled, frequency, time_of_day, day_of_week?, day_of_month?, timezone }`. Server builds the cron from friendly fields, validates, computes `next_run_at`. |
| `DELETE` | `/api/sources/{id}/schedule` | Removes the schedule row. |

The UI sends **friendly fields**, not raw cron. The server maps `frequency ∈ {hourly, daily, weekly, monthly}` (+ time/day) → cron, keeping the cron string an internal detail (raw-cron field addable later without a table change). New `app/schemas/schedule.py` (`ScheduleConfig`/`ScheduleResponse`); new `app/models/schedule.py` registered in `app/models/__init__.py` (model-import invariant).

### Manual trigger — behavior change, same contract

`POST /api/extraction/trigger/{source_id}` stops dispatching a `BackgroundTask` and instead **inserts a `pending` run** (`trigger='manual'`); the worker picks it up. It still returns `{run_id, source_id, status, message}`; `status` now begins as `pending` and `/runs/{id}` polling shows `pending → running → completed`. The 409-on-active behavior is preserved, now enforced by catching the `uq_active_run_per_source` unique-violation instead of checking `source.status`.

`GET /api/extraction/runs*` responses gain `trigger`.

### Frontend

- **`ScheduleControl`** on the source view: Off / Hourly / Daily\@time / Weekly\@day+time / Monthly\@day+time, timezone selector (default browser tz), "Next run: …" line + last-run status. Save calls `PUT`. Petrol-ink / signal-amber design system (matches `ExportPanel`/`ChangelogPanel`).
- **`types/index.ts`**: add `Schedule`/`ScheduleConfig`; extend `ExtractionRun.status` with `"pending"`, add `trigger: "manual" | "scheduled"`.
- **`api/client.ts`**: `getSchedule`, `putSchedule`, `deleteSchedule`.
- **Run display:** add a **PENDING** ("Queued…") state to the run indicator and a manual/scheduled tag on run-history rows. Polling flow unchanged.

No new top-level view — a schedule panel on the existing source view plus the queued-state polish.

## Testing

Repo conventions: sync `psycopg2`/`Session` for DB-logic tests; `httpx.AsyncClient` with a per-test `NullPool` async engine for route tests.

- **`test_schedule.py`** (pure): friendly fields → cron for each frequency; `next_run_at` computed in tz and stored UTC (incl. DST); catch-up recomputes forward exactly once.
- **`test_queue.py`** (sync DB): `uq_active_run_per_source` rejects a second active run per source; `FOR UPDATE SKIP LOCKED` claim — two concurrent claims never grab the same row; reaper requeues stale `running` (and fails at cap), leaves fresh-heartbeat rows alone.
- **`test_scheduler.py`**: due+enabled+idle → one `pending` run (`trigger='scheduled'`), `next_run_at` advances, `last_run_id` set; due+active → coalesce (no new row), still advances; disabled → never enqueues.
- **`test_worker.py`**: claims pending, calls `extract_source` (Firecrawl mocked), marks completed; on exception → failed/requeue under cap.
- **Route changes** (`test_integration.py` / `test_defects.py`): `POST /trigger` leaves a `pending` run; second trigger while active → 409; `/runs` exposes `trigger`; schedule CRUD round-trips and rejects invalid input.

Target: green `pytest` (currently 43) plus new suites; `npm run build` + `npm run lint` clean.

## Rollout

1. Alembic revision (table + columns + enum value + two indexes); backfill `trigger='manual'`.
2. Land worker/scheduler entrypoints + route refactor (web enqueues, no longer executes).
3. Add `worker` + `scheduler` services to `docker-compose.yml` (same image, different `command:`); add `croniter` to `requirements.txt`.
4. **Verify end-to-end** on live Clumio (deploy is rebuild-to-test per the docker-compose workflow): manual trigger → `pending`→`running`→`completed` via a worker; near-future daily schedule enqueues at the boundary, coalesces on overlap, `next_run_at` advances; kill a worker mid-run → reaper requeues.

**Compatibility:** the queue lives in `extraction_runs`, so old completed runs are untouched; the only behavior change is manual triggers becoming async-via-queue (transparent to the polling UI). No content/version data migration.

## Out of scope (this round)

- Kubernetes manifests / Helm chart (app is K8s-shaped; manifests are a follow-on).
- PDF export (separate goal, separate spec).
- Per-source multiple schedules (one schedule per source for now).
- Raw-cron UI field (engine supports it; UI can add later without migration).
