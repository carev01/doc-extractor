# DocExtractor — API Reference

All endpoints are served at `/api/*`. Interactive Swagger UI is available at `/docs`
when the backend is running.

## Authentication

Authentication is opt-in (disabled when `DOCEXTRACTOR_AUTH_JWT_SECRET` is empty).

When enabled, every `/api/` request must include either:
- `X-API-Key: <key>` — API key authentication
- `Authorization: Bearer <jwt>` — JWT authentication

### RBAC

| Method | Minimum Role |
|--------|-------------|
| GET, HEAD | `read_only` |
| POST, PUT, PATCH, DELETE | `read_write` |

Admin-only endpoints (user management, jobs, auth realms) require `admin`.

---

## Vendors

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/vendors` | List vendors (paginated, `offset` + `limit`) |
| `POST` | `/api/vendors` | Create a vendor (`name`, `website`) |
| `GET` | `/api/vendors/{id}` | Get a vendor |
| `PATCH` | `/api/vendors/{id}` | Update a vendor |
| `DELETE` | `/api/vendors/{id}` | Delete a vendor |

---

## Products

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/products` | List products (optional `vendor_id` filter) |
| `POST` | `/api/products` | Create a product under a vendor |
| `GET` | `/api/products/{id}` | Get a product |
| `PATCH` | `/api/products/{id}` | Update a product |
| `DELETE` | `/api/products/{id}` | Delete a product |
| `POST` | `/api/products/{id}/versions/enable` | Enable versioning for a product |
| `POST` | `/api/products/{id}/versions/bump` | Bump product version |

---

## Sources

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/sources` | List sources (optional `product_id` filter) |
| `POST` | `/api/sources` | Create a documentation source (web) |
| `POST` | `/api/sources/pdf` | Create a PDF source (upload PDF) |
| `PUT` | `/api/sources/{id}/pdf` | Replace PDF for an existing source |
| `GET` | `/api/sources/{id}` | Get a source |
| `PATCH` | `/api/sources/{id}` | Update a source |
| `DELETE` | `/api/sources/{id}` | Delete a source |
| `GET` | `/api/sources/pickable` | List sources available for export/jobs |
| `POST` | `/api/sources/import` | Bulk import sources |
| `POST` | `/api/sources/{id}/detect-version-token` | Auto-detect version token from page |
| `GET` | `/api/sources/{id}/changelog` | Get changelog (versioned article diffs) |
| `GET` | `/api/sources/{id}/browse` | Browse articles by TOC structure |

---

## Extraction

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/extraction/trigger/{source_id}` | Trigger an extraction run |
| `GET` | `/api/extraction/runs` | List runs (optional `source_id` filter) |
| `GET` | `/api/extraction/runs/{run_id}` | Get run status |
| `GET` | `/api/extraction/runs/{run_id}/logs` | Get run logs |
| `POST` | `/api/extraction/runs/{run_id}/cancel` | Cancel a running extraction |
| `POST` | `/api/extraction/runs/{run_id}/pause` | Pause a running extraction |
| `POST` | `/api/extraction/runs/{run_id}/resume` | Resume a paused extraction |
| `POST` | `/api/extraction/runs/{run_id}/retry-escalation` | Retry VLM escalation for failed pages |
| `POST` | `/api/extraction/resanitize/{source_id}` | Re-sanitize all articles for a source |

---

## Articles

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/articles` | Search articles (`source_id`, `q`, `offset`, `limit`) |
| `GET` | `/api/articles/{id}` | Get article detail (with content + images) |
| `GET` | `/api/articles/delta` | Streaming JSONL delta feed for programmatic sync (see [Delta Feed](#delta-feed)) |
| `GET` | `/api/articles/toc/{source_id}` | Get TOC tree for a source |
| `GET` | `/api/articles/{id}/versions` | List article versions |
| `GET` | `/api/articles/{id}/versions/{version_id}` | Get a specific version |
| `GET` | `/api/articles/{id}/versions/{version_id}/diff` | Diff a version against the next or current version (`against=next\|current`) |

---

## Delta Feed

`GET /api/articles/delta` streams article changes as **JSON Lines** (`application/x-ndjson`) for a downstream consumer (e.g. a graph-RAG indexer) to bootstrap from and then keep in sync. It is the reliable, pull-based data channel; the `extraction_complete` [webhook](#webhooks) is the "wake up and pull" nudge.

**Query parameters**

| Param | Description |
|-------|-------------|
| `since` | Opaque cursor from a prior pull's control record. **Omit for a full bootstrap snapshot.** |
| `source_id` | Optional — restrict to one source |
| `vendor_id` | Optional — restrict to one vendor (sharding) |

- **`since` omitted → bootstrap**: streams every current, visible, non-removed article as `change_type: "added"` (with `seq: null`); the control record's `next_since` is the watermark to continue from.
- **`since` present → incremental**: streams `added`/`updated` content records and `removed` tombstones since that watermark.

The response is RBAC-filtered to the caller's visible vendors. The **last line is always a control record** — `{"control":"cursor","next_since":"…","count":N}`. A truncated stream lacks it, so clients must only advance their stored cursor on a clean finish. Ordering is gap-free even when extraction runs overlap (see [Architecture → Delta Feed](ARCHITECTURE.md#delta-feed)).

**Content record** (`added` / `updated`):

```json
{
  "seq": 4811,
  "change_type": "updated",
  "id": "article-uuid",
  "topic_key": "https://help.example.com/backup/proxies",
  "source_id": "source-uuid",
  "vendor": "Veeam",
  "product": "Backup & Replication",
  "title": "Backup Proxies",
  "source_url": "https://help.example.com/backup/proxies",
  "last_updated_at": "2026-07-01T09:12:00Z",
  "content_hash": "…",
  "estimated_tokens": 1234,
  "parent_chapter": "Deployment",
  "top_level_chapter": "Installation",
  "sort_order": 42,
  "run_id": "run-uuid",
  "content_markdown": "# Backup Proxies\n\n…",
  "images": [{ "url": "/media/…/x.png", "alt": "topology", "description": null, "kind": null }]
}
```

**Tombstone record** (`removed`):

```json
{ "seq": 4830, "change_type": "removed", "id": "article-uuid", "topic_key": "…",
  "source_id": "source-uuid", "removed_at": "2026-07-09T22:04:00Z", "run_id": "run-uuid" }
```

`images[].description` / `kind` are reserved for VLM-generated image descriptions and are currently `null`.

**Typical consumer loop**

1. Bootstrap: `GET /api/articles/delta` → apply every record, store `next_since`.
2. Subscribe a webhook to `extraction_complete`.
3. On each notification, `GET /api/articles/delta?since=<stored cursor>` → upsert `added`/`updated`, delete `removed`, then persist the new `next_since`. Use your own stored cursor (not the one in the webhook), so a missed delivery self-heals on the next pull.

---

## Export

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/export` | Create an export job |
| `GET` | `/api/export/jobs` | List export jobs |
| `GET` | `/api/export/jobs/{job_id}` | Get export job status |
| `POST` | `/api/export/jobs/{job_id}/cancel` | Cancel a pending export |
| `GET` | `/api/export/download/{export_id}` | Download an export (zip) |
| `GET` | `/api/export/download/{export_id}/{filename}` | Download a single file |
| `GET` | `/api/export/list` | List completed exports |
| `DELETE` | `/api/export/{export_id}` | Delete an export |

### Export Request Body

```json
{
  "source_id": "uuid",
  "format": "markdown",
  "article_ids": ["uuid1", "uuid2"],
  "split_max_articles": 50,
  "split_max_bytes": 10485760,
  "split_max_tokens": 100000
}
```

- `article_ids` — optional; omit for full export
- `split_*` — optional; omit for single file
- `format` — `"markdown"` or `"pdf"`

---

## Jobs (Scheduled Extraction)

Admin-only.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/jobs` | List jobs |
| `POST` | `/api/jobs` | Create a scheduled job |
| `GET` | `/api/jobs/{id}` | Get a job |
| `PATCH` | `/api/jobs/{id}` | Update a job (enable/disable, change schedule) |
| `DELETE` | `/api/jobs/{id}` | Delete a job |
| `PUT` | `/api/jobs/{id}/sources` | Set sources for a job |
| `PUT` | `/api/jobs/{id}/sources/{source_id}` | Add a source to a job |
| `DELETE` | `/api/jobs/{id}/sources/{source_id}` | Remove a source from a job |
| `POST` | `/api/jobs/{id}/run` | Trigger an immediate run |
| `GET` | `/api/jobs/{id}/runs` | List runs for a job |
| `GET` | `/api/jobs/runs` | List all job runs |

---

## Auth

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/auth/status` | Check if auth is enabled |
| `POST` | `/api/auth/register` | Register a new user (admin-only after first user) |
| `POST` | `/api/auth/login` | Login (email + password) |
| `POST` | `/api/auth/refresh` | Refresh access token |
| `GET` | `/api/auth/me` | Get current user |
| `POST` | `/api/auth/change-password` | Change password |
| `GET` | `/api/auth/keys` | List my API keys |
| `POST` | `/api/auth/keys` | Create an API key |
| `POST` | `/api/auth/keys/{id}/rotate` | Rotate an API key |
| `DELETE` | `/api/auth/keys/{id}` | Revoke an API key |
| `GET` | `/api/auth/admin/keys` | List all API keys (admin-only) |
| `GET` | `/api/auth/users` | List users (admin-only) |
| `GET` | `/api/auth/oauth/{provider}/authorize` | Get OAuth2 authorize URL |
| `GET` | `/api/auth/oauth/{provider}/callback` | OAuth2 callback |

---

## Auth Realms (Admin-only)

Stored credentials for authenticated scraping.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/auth-realms` | Create an auth realm |
| `GET` | `/api/auth-realms` | List auth realms |
| `GET` | `/api/auth-realms/{id}` | Get an auth realm |
| `PATCH` | `/api/auth-realms/{id}` | Update an auth realm |
| `DELETE` | `/api/auth-realms/{id}` | Delete an auth realm |
| `POST` | `/api/auth-realms/{id}/login` | Trigger a login flow |
| `POST` | `/api/auth-realms/{id}/session` | Store a session |
| `POST` | `/api/auth-realms/{id}/test` | Test stored credentials |

---

## Webhooks

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/webhooks` | List webhooks |
| `POST` | `/api/webhooks` | Create a webhook |
| `GET` | `/api/webhooks/{id}` | Get a webhook |
| `PATCH` | `/api/webhooks/{id}` | Update a webhook |
| `DELETE` | `/api/webhooks/{id}` | Delete a webhook |
| `POST` | `/api/webhooks/{id}/test` | Send a test delivery |
| `GET` | `/api/webhooks/{id}/deliveries` | List webhook deliveries |

**Event types** (a webhook subscribes to any subset): `new_page`, `updated_page`, `removed_page`, `extraction_complete`. Payloads are POSTed as JSON, signed with `X-DocExtractor-Signature: sha256=…` (HMAC of the body with the webhook's secret), and retried with backoff.

The `extraction_complete` payload carries a `delta` summary so a consumer knows whether to pull the [Delta Feed](#delta-feed):

```json
{
  "event": "extraction_complete",
  "run_id": "…", "source_id": "…",
  "source_name": "…", "vendor_name": "…", "product_name": "…",
  "delta": { "added": 12, "updated": 3, "removed": 1, "watermark": "eyJ2IjoxLCJzZXEiOjQ4MzB9" }
}
```

The `watermark` is informational; consumers should pull with their own stored cursor.

---

## Dashboard

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/dashboard/sources` | Get dashboard overview (source health scores, counts) |

---

## Profiles

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/profiles` | List available extraction profiles |

---

## Health

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | Health check (no auth required) |