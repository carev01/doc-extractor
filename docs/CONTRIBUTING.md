# Contributing to DocExtractor

## Development Setup

See the [README](../README.md#quick-start) for local development setup instructions.

## Workflow

1. **Create a branch** from `main`:
   ```bash
   git checkout main
   git pull origin main
   git checkout -b feat/your-feature
   ```

2. **Make your changes** — follow the conventions below.

3. **Test locally**:
   ```bash
   # Backend
   cd backend && pytest -q

   # Frontend
   cd frontend && npm run build && npm run lint
   ```

4. **Push and open a PR**:
   ```bash
   git push -u origin feat/your-feature
   gh pr create --base main --title "feat: ..." --body "..."
   ```

CI runs automatically on every PR: backend tests (PostgreSQL 16 + pytest), and
on `main` it also builds and pushes Docker images.

## Branch Naming

| Prefix | Use |
|--------|-----|
| `feat/` | New features |
| `fix/` | Bug fixes |
| `chore/` | Maintenance, deps, config |
| `refactor/` | Code restructuring without behavior change |
| `perf/` | Performance improvements |
| `docs/` | Documentation only |

## Backend Conventions

### Project Structure

```
backend/app/
├── core/        # Database, config, auth, security
├── models/      # SQLAlchemy ORM models
├── schemas/     # Pydantic request/response schemas
├── routes/      # FastAPI routers (thin — call services)
├── services/    # Business logic (thick — all extraction/export logic)
└── ...
```

### Key Rules

1. **Import all models in `app/models/__init__.py`** — `Base.metadata` must be
   populated before `create_all` runs on startup. Adding a new model file?
   Add it to the `__init__.py` imports.

2. **Routes are thin** — Routers validate input, call a service, and return a
   schema. Business logic lives in `app/services/`.

3. **Extraction uses pre-created run IDs** — The route creates the `ExtractionRun`
   row and passes its `id` to the background task. Never create a second run row
   for the same extraction.

4. **Tests use sync DB** — `tests/` use `psycopg2` (not `asyncpg`) to avoid
   pytest-asyncio event-loop conflicts. The test database is `docextractor_test`.

5. **Settings via env vars** — All configuration goes through `pydantic-settings`
   with the `DOCEXTRACTOR_` prefix. Never hardcode config values.

### Database Migrations

```bash
cd backend
alembic upgrade head                         # Apply migrations
alembic revision --autogenerate -m "message" # Generate a migration
```

Always review auto-generated migrations before committing — Alembic can miss
constraints or generate unnecessary diffs.

### Adding a New Documentation Platform Profile

1. Create `app/services/profiles/your_platform.py`
2. Implement the profile interface (detection, TOC discovery, content scraping)
3. Register it in `app/services/profiles/registry.py`
4. Add a test in `backend/tests/`

## Frontend Conventions

- **React 19 + TypeScript** — Strict typing, no `any` without justification.
- **Single `App.css`** — All styles in one file using CSS custom properties.
- **API calls** — All go through `src/api/client.ts`. Types in `src/types/index.ts`.
- **Views** — Page-level components in `src/views/`. Smaller components in `src/components/`.

## Testing

- **139 test files** covering extraction, export, versioning, auth, profiles,
  PDF pipeline, webhooks, scheduling, and more.
- Tests require a running PostgreSQL with a `docextractor_test` database.
- The CI pipeline creates this database automatically; for local testing:
  ```bash
  psql -U docextractor -d docextractor -c 'CREATE DATABASE docextractor_test;'
  ```

## Key Invariants

These must never be violated:

1. **Models imported before `create_all`** — `app/main.py` imports `app.models`
   (not individual models) at startup.
2. **Run IDs are pre-created** — Never create a second `ExtractionRun` row.
3. **Split never breaks articles** — `ExportEngine._split_articles` guarantees
   an individual article is never split across output files.
4. **Firecrawl fast-fail** — `_check_available()` uses a 5s connect timeout to
   fail quickly when Firecrawl is down, instead of hanging for 300s.

## CI

- **On PR**: PostgreSQL 16 service → `alembic upgrade head` → `pytest -q`
- **On `main`/tags**: Also builds and pushes Docker images to GHCR
- WeasyPrint system libraries are installed in CI to match the Docker image