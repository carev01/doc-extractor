# Kubernetes Deployment (Helm chart) — Design

**Date:** 2026-06-18
**Status:** Approved (design)
**Goal:** Deploy DocExtractor on the homelab **k3s** cluster via a Helm chart (+ rendered plain manifests). This is the last open CLAUDE.md goal.

## Source topology (from `docker-compose.yml`)

Five services to translate:
- **postgres** (`postgres:16-alpine`) — DB `docextractor`, PVC for data.
- **backend** (web, FastAPI/uvicorn `:8000`) — runs `alembic upgrade head && uvicorn`; mounts `exports/` + `media/`.
- **worker** (`python -m app.worker`) — job-queue worker; mounts `exports/` + `media/`.
- **scheduler** (`python -m app.scheduler`) — recurrent-run scheduler; no volumes.
- **frontend** (nginx serving the Vite `dist/`) — `nginx.conf` proxies `/api/` + `/media/` to the backend and SPA-falls-back to `index.html`.

Shared `exports/` + `media/` are written by the **worker** and served by the **backend**, so both need the *same* volume. Firecrawl is **external**, already running in the same k3s cluster (`firecrawl.k3s.home.lan`). `/api/health` already exists for probes.

## Environment decisions (confirmed)

| Decision | Choice |
|---|---|
| PostgreSQL | **In-cluster StatefulSet** (gated by `postgres.enabled`; external DB via `externalDatabaseUrl` when off). |
| Shared storage | **VMware CSI, RWO block only** → backend + worker **co-located on one node** (podAffinity), each **replicas:1**, sharing RWO `exports`/`media` PVCs. RWX (vSAN File/Longhorn/NFS) is a future values change. |
| Images | **ghcr.io/carev01/doc-extractor-{backend,frontend}**, built+pushed by a **GitHub Actions** workflow. Repo public → anonymous pulls (no imagePullSecret by default). |
| Ingress | **Traefik, HTTP** on `docextractor.k3s.home.lan`; `tls` is a values-toggle for later. |
| Secrets | Plain k8s **Secret** driven by values (swappable for sealed/external-secrets later — noted, not built). |
| Migrations | **Backend `migrate` init container** runs `alembic upgrade head` before the app container starts (see Migrations §, amended). |

## Chart structure

```
deploy/
  helm/docextractor/
    Chart.yaml
    values.yaml                # documented defaults
    values-homelab.yaml        # concrete: ghcr images, RWO class, host, etc.
    .helmignore
    templates/
      _helpers.tpl             # name/labels helpers
      configmap.yaml           # non-secret env
      secret.yaml              # db password, firecrawl key, llm key (from values)
      postgres-statefulset.yaml
      postgres-service.yaml    # headless
      backend-deployment.yaml
      backend-service.yaml     # ClusterIP :8000
      worker-deployment.yaml   # podAffinity -> backend
      scheduler-deployment.yaml
      frontend-deployment.yaml
      frontend-service.yaml    # ClusterIP :80
      frontend-nginx-configmap.yaml  # templated nginx.conf (upstream = backend Service)
      ingress.yaml             # Traefik, host, tls toggle
      pvc-exports.yaml         # RWO
      pvc-media.yaml           # RWO
      NOTES.txt
  rendered/                    # `helm template -f values-homelab.yaml` output (committed reference)
  README.md                    # install/upgrade runbook
```

## Component details

**Postgres** — StatefulSet (1 replica) with a `volumeClaimTemplate` (RWO, `storageClassName` from values), headless Service `postgres`. Env `POSTGRES_DB/USER/PASSWORD` (password from Secret). `pg_isready` readiness/liveness. Gated by `postgres.enabled`.

**Backend (web)** — Deployment `replicas:1`, ClusterIP Service `:8000`. Mounts `exports` PVC at `/app/exports` and `media` PVC at `/app/media`. Env from ConfigMap + Secret. Command: `uvicorn app.main:app --host 0.0.0.0 --port 8000` (migrations moved to the Job). Readiness+liveness HTTP `GET /api/health`.

**Worker** — Deployment `replicas:1`, `command: ["python","-m","app.worker"]`, mounts the same `exports`/`media` PVCs. **podAffinity** (`requiredDuringScheduling`, topologyKey `kubernetes.io/hostname`) matching the backend pod labels, so it co-schedules on the backend's node (required because RWO volumes attach to a single node). Liveness via a lightweight `exec` (process check).

**Scheduler** — Deployment `replicas:1`, `command: ["python","-m","app.scheduler"]`, no volumes. Singleton (the queue uses `FOR UPDATE SKIP LOCKED`, but one scheduler avoids duplicate dispatch).

**Frontend** — Deployment + ClusterIP Service `:80`. `nginx.conf` is supplied via a **templated ConfigMap** (mounted at `/etc/nginx/conf.d/default.conf`) so the `proxy_pass` upstream points at the backend Service DNS within the release namespace (and keeps the 600s `/api` read timeout + `/media` proxy + SPA fallback). HTTP readiness/liveness on `/`.

**Ingress** — Traefik (`ingressClassName: traefik`), host from values (`docextractor.k3s.home.lan`), routes `/` → frontend Service. TLS block rendered only when `ingress.tls.enabled`. (All `/api` + `/media` routing stays inside the frontend nginx, preserving same-origin.)

## Config & secrets

**ConfigMap** (non-secret): `DOCEXTRACTOR_FIRECRAWL_API_URL`, `DOCEXTRACTOR_WEBHOOK_BASE_URL` = `http://<release>-backend.<namespace>.svc.cluster.local:8000` (Firecrawl → backend Service directly), `DOCEXTRACTOR_CORS_ORIGINS` (includes `http://docextractor.k3s.home.lan`), `DOCEXTRACTOR_LLM_PROVIDER`/`_MODEL`/`_BASE_URL`/`_FALLBACK_ENABLED`. The async+sync DB URLs are built from the postgres Service host + DB name, with the password injected from the Secret via `valueFrom.secretKeyRef` (or composed at runtime).

**Secret** (from values, base64 at render): `DOCEXTRACTOR_DATABASE_URL`, `DOCEXTRACTOR_DATABASE_URL_SYNC`, `DOCEXTRACTOR_FIRECRAWL_API_KEY`, `DOCEXTRACTOR_LLM_API_KEY`, and `postgres-password` (consumed by the postgres StatefulSet). The two DB URLs embed the password, so they live in the Secret in full — a `_helpers.tpl` partial renders `postgresql+asyncpg://<user>:<password>@<svc>:5432/<db>` and the `+psycopg2` sync variant from values (user/password/db/service), keeping all password material out of the ConfigMap. All app pods (backend/worker/scheduler/migration-Job) load env via `envFrom: [configMapRef, secretRef]`. When `postgres.enabled=false`, the two DB URL secret entries come from `externalDatabaseUrl`/`externalDatabaseUrlSync` values instead.

## Migrations

> **Amended during implementation.** The original plan used a Helm `pre-install,pre-upgrade` hook
> Job. That fails a *fresh* install: hook resources run before non-hook resources, so the migrate
> Job runs before the (non-hook) Postgres StatefulSet exists and aborts the install. Rather than make
> Postgres a hook (which breaks upgrade/uninstall lifecycle), migrations moved into a backend pod
> **init container**.

The backend Deployment has an init container `migrate` (backend image, same `envFrom` config+secret,
command `alembic upgrade head`) that runs before the `backend` container. If Postgres is not yet
reachable the init container exits non-zero and the kubelet retries it with backoff until Postgres is
ready — so no Helm hook ordering or explicit wait loop is needed, on either install or upgrade. The
app's startup `create_all()` is a no-op once Alembic has built the schema (`checkfirst`). The worker
and scheduler Deployments get a `wait-for-backend` init container (HTTP-polls the backend
`/api/health`) so they only start once the schema exists. With no hook resources left, the ConfigMap
and Secret are plain release resources (cleaned up on `helm uninstall`). Backend Dockerfile `CMD` is
uvicorn-only.

## Images & CI

`.github/workflows/images.yml`: on push to `main` and on `v*` tags, build+push `ghcr.io/carev01/doc-extractor-backend` (context `backend/`) and `-frontend` (context `frontend/`, build-arg `VITE_API_BASE_URL=""`) via `docker/build-push-action`, tagged with the git SHA and `latest` (and the version tag when tagged). Uses `GITHUB_TOKEN` with `packages: write`. Chart `values.image.{backend,frontend}.{repository,tag,pullPolicy}` default to these; `imagePullSecret` values block included but default-empty (public packages).

## Required app changes (small, flagged)

1. `backend/Dockerfile`: `CMD` → `uvicorn app.main:app --host 0.0.0.0 --port 8000` (migration moves to the Helm Job).
2. CORS: ensure the ingress host is in the allowlist (via `DOCEXTRACTOR_CORS_ORIGINS` config — no code change if already env-driven; otherwise add the host).

No other application code changes; the filesystem-based `exports/`/`media/` model is preserved (shared RWO PVCs).

## Validation / testing

No unit-test surface (declarative YAML). Validation in this environment:
- `helm lint deploy/helm/docextractor`
- `helm template -f values-homelab.yaml` renders without error; commit the output to `deploy/rendered/`.
- Schema-validate the rendered manifests with `kubeconform` (or `kubectl apply --dry-run=client`) if available.
The actual `helm install`/`upgrade` against the k3s cluster + smoke (frontend reachable on the host, an extraction runs, exports download) is the **user's verification step** (out of this environment's reach); `deploy/README.md` provides the runbook.

## Out of scope / future
- RWX storage + horizontal scaling of backend/worker (documented values change).
- TLS/cert-manager (values toggle, not enabled).
- Sealed/external secrets, HPA, NetworkPolicies, object-storage backend for exports/media.
