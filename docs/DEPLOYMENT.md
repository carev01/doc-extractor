# DocExtractor — Deployment Guide

## Docker Compose (Local / Single-Host)

The simplest way to run DocExtractor. All five services (PostgreSQL, backend, worker,
scheduler, frontend) are defined in [`docker-compose.yml`](../docker-compose.yml).

### Prerequisites

- Docker Engine + Docker Compose
- A Firecrawl instance reachable from the Docker network

### Steps

```bash
git clone https://github.com/carev01/doc-extractor.git
cd doc-extractor

# Review and adjust environment in docker-compose.yml:
#   - DOCEXTRACTOR_FIRECRAWL_API_URL
#   - DOCEXTRACTOR_FIRECRAWL_API_KEY
#   - DOCEXTRACTOR_WEBHOOK_BASE_URL

# Start everything
docker compose up -d

# Check health
curl http://localhost:8000/api/health
# → {"status":"ok","version":"0.1.0"}

# Frontend: http://localhost:3000
# Backend API: http://localhost:8000
# Swagger UI: http://localhost:8000/docs
```

### Services

| Service | Port | Description |
|---------|------|-------------|
| `postgres` | 5432 | PostgreSQL 16 (data in `postgres_data` volume) |
| `backend` | 8000 | FastAPI app (migrations run automatically on start) |
| `worker` | — | Background extraction + export runner |
| `scheduler` | — | Cron tick + dead-run reaper |
| `frontend` | 3000 | nginx serving the built React SPA |

### Volumes

| Volume | Mount | Purpose |
|--------|-------|---------|
| `postgres_data` | `/var/lib/postgresql/data` | Database |
| `exports_data` | `/app/exports` | Generated exports |
| `media_data` | `/app/media` | Article images |

### Stopping

```bash
docker compose down          # Stop services, keep volumes
docker compose down -v       # Stop services, delete volumes (⚠️ data loss)
```

---

## Kubernetes (K3s)

A Helm chart is provided in [`deploy/helm/docextractor`](../deploy/helm/docextractor).

### Prerequisites

- K3s (or any k8s 1.28+) with Traefik ingress
- A `ReadWriteOnce` StorageClass (default) or `ReadWriteMany` for scaling
- Firecrawl running in the cluster or reachable from it
- Container images published to a registry (GHCR by default)

### Install

```bash
kubectl create namespace docextractor

helm upgrade --install docextractor deploy/helm/docextractor \
  --namespace docextractor \
  -f deploy/helm/docextractor/values-homelab.yaml \
  --set postgres.password='<db-password>' \
  --set firecrawl.apiKey='<firecrawl-key>' \
  --set storage.storageClassName='<your-storageclass>' \
  --set postgres.storageClassName='<your-storageclass>'
```

Add the ingress host to DNS or `/etc/hosts`:
```
<traefik-ingress-ip>  docextractor.k3s.home.lan
```

### Verify

```bash
kubectl -n docextractor get pods,svc,ingress,pvc
curl -H 'Host: docextractor.k3s.home.lan' http://<traefik-ip>/api/health
```

### Helm Values

Key values (see [`values.yaml`](../deploy/helm/docextractor/values.yaml) for all):

| Value | Default | Description |
|-------|---------|-------------|
| `image.repository` | `ghcr.io/carev01/doc-extractor-{backend,frontend}` | Image registry |
| `image.tag` | `latest` | Image tag |
| `postgres.password` | — | PostgreSQL password (required) |
| `firecrawl.apiUrl` | `http://firecrawl.k3s.home.lan` | Firecrawl URL |
| `firecrawl.apiKey` | — | Firecrawl API key |
| `storage.accessMode` | `ReadWriteOnce` | Volume access mode |
| `storage.storageClassName` | — | StorageClass for exports/media PVCs |
| `ingress.host` | `docextractor.k3s.home.lan` | Ingress hostname |
| `ingress.tls.enabled` | `false` | Enable TLS |
| `llm.fallbackEnabled` | `false` | Enable LLM profile derivation |

### Storage Notes

- **RWO (default)**: Backend + worker are pinned to 1 replica and co-scheduled
  (podAffinity) to share the `exports`/`media` volumes.
- **RWX**: To scale backend/worker, switch to `ReadWriteMany`:
  ```bash
  --set storage.accessMode=ReadWriteMany --set storage.storageClassName=<rwx>
  ```

### Migrations

Migrations run automatically in the backend pod's `migrate` init container
(`alembic upgrade head`) before the app container starts. If Postgres isn't ready,
the init container retries until it is.

```bash
# Inspect migration logs
kubectl logs deploy/docextractor-backend -c migrate -n docextractor
```

### Uninstall

```bash
helm uninstall docextractor --namespace docextractor
```

> **Note:** `helm uninstall` deletes PVCs for exports/media (contents lost).
> The Postgres PVC (from StatefulSet `volumeClaimTemplate`) survives — delete it
> manually or `kubectl delete namespace docextractor` to reclaim.

---

## Enabling Authentication

### JWT Auth

1. Generate a JWT secret:
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

2. Set it as an environment variable:
   ```bash
   DOCEXTRACTOR_AUTH_JWT_SECRET=<your-secret>
   ```

3. The first user to register via `POST /api/auth/register` becomes `admin`.

### OAuth2 (Google / Okta)

```bash
DOCEXTRACTOR_AUTH_GOOGLE_CLIENT_ID=<client-id>
DOCEXTRACTOR_AUTH_GOOGLE_CLIENT_SECRET=<client-secret>
DOCEXTRACTOR_AUTH_OKTA_CLIENT_ID=<client-id>
DOCEXTRACTOR_AUTH_OKTA_CLIENT_SECRET=<client-secret>
DOCEXTRACTOR_AUTH_OKTA_DOMAIN=<your-domain>.okta.com
DOCEXTRACTOR_AUTH_OAUTH_REDIRECT_BASE=http://localhost:5173
```

### Encrypted Auth Realms

To store credentials for authenticated scraping, generate a Fernet key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set it as `DOCEXTRACTOR_SECRET_KEY`.

---

## External Services

### Firecrawl

Required for web scraping. Deploy a local instance or use a hosted one.

- URL: set via `DOCEXTRACTOR_FIRECRAWL_API_URL`
- API Key: set via `DOCEXTRACTOR_FIRECRAWL_API_KEY` (not needed for local instances without auth)
- Webhook callback: set `DOCEXTRACTOR_WEBHOOK_BASE_URL` to the URL Firecrawl can reach your backend at (for per-page progress events). Leave empty to use polling instead.

### Browserless (optional)

For JS-rendered SPAs (e.g., Salesforce Help). The default URL points to an
in-cluster service.

```bash
DOCEXTRACTOR_BROWSERLESS_URL=http://browserless.browserless.svc.cluster.local:3000
DOCEXTRACTOR_BROWSERLESS_TOKEN=<token>
```

### Docling-serve (optional, for PDF)

Converts PDFs to Markdown. Can run as a separate service.

```bash
DOCEXTRACTOR_DOCLING_SERVE_URL=http://docling.home.lan
DOCEXTRACTOR_DOCLING_SERVE_API_KEY=<key>
```

### VLM escalation (optional, for PDF)

Improves conversion quality for complex PDF layouts via a vision-language model.

```bash
DOCEXTRACTOR_PDF_VLM_ESCALATION_ENABLED=true
DOCEXTRACTOR_PDF_VLM_API_KEY=<openrouter-key>
DOCEXTRACTOR_PDF_VLM_MODEL=qwen/qwen3-vl-32b-instruct
```

### Image descriptions (optional)

Describes meaningful scraped images with a VLM and surfaces the descriptions in the API and delta feed (see [Architecture → Image enrichment](ARCHITECTURE.md#design-decisions)). Opt-in and best-effort; a per-run budget bounds cost.

```bash
DOCEXTRACTOR_IMAGE_VLM_ENABLED=true
DOCEXTRACTOR_IMAGE_VLM_API_KEY=<openrouter-key>
DOCEXTRACTOR_IMAGE_VLM_MODEL=qwen/qwen3-vl-32b-instruct
DOCEXTRACTOR_IMAGE_VLM_MAX_PER_RUN=100   # cap new descriptions per run
```

The worker mounts the `media_data` volume (where images live), so it — not the backend — runs the enrichment phase. On first enable, a large source's backlog is described over several runs (bounded by the budget).

---

## CI/CD

The GitHub Actions workflow (`.github/workflows/ci.yml`) handles:

1. **Testing**: Runs `pytest` against PostgreSQL 16 on every PR and push to `main`
2. **Building**: On `main` and tags, builds and pushes Docker images to GHCR:
   - `ghcr.io/carev01/doc-extractor-backend:latest`
   - `ghcr.io/carev01/doc-extractor-frontend:latest`
   - Also tagged with Git SHA and tag name

### Pulling Images

```bash
docker pull ghcr.io/carev01/doc-extractor-backend:latest
docker pull ghcr.io/carev01/doc-extractor-frontend:latest
```

Make the GHCR packages public, or configure `imagePullSecrets` in the Helm values.