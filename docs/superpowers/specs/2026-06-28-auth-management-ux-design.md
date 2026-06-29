# Auth-management UX: Cookie-Editor upload, source realm assignment, expiry monitoring

**Date:** 2026-06-28
**Status:** Design — approved; pending spec review
**Area:** `backend/app/routes/auth_realms.py`, `backend/app/schemas/auth_realm.py`, `backend/app/routes/extraction.py`, `backend/app/services/auth/` (helper); `frontend/src/views/Logins.tsx`, `frontend/src/components/SourceList.tsx`, `frontend/src/api/client.ts`, `frontend/src/types/index.ts`

Operational controls for authenticated sources, surfaced in the existing UI. Three independent features in one spec (they share the auth-realm surface):

1. Upload the cookie JSON exported by the **Cookie-Editor** browser plugin.
2. Assign / change a source's auth realm **after** the source exists.
3. **Cookie-expiration monitoring** — display soonest expiry, and **block** an extraction whose realm session has already expired.

No DB schema change: session cookies already live in the realm's encrypted `state_snapshot`; expiry is computed from them.

## Background (current state)

- `Logins.tsx` already: creates realms, lists them with a status badge (`active`/`needs_login`/`expired`/`login_failed`), runs scripted login, deletes, and uploads a session via a textarea that requires the Playwright `{cookies, origins}` shape.
- `SourceList.tsx` already: shows a realm dropdown when **creating** a source; there's no way to set/change a realm on an existing source.
- `POST /sources/{id}` (PATCH) already accepts `auth_realm_id` — so feature 2 is frontend-only.
- `AuthRealm.state_snapshot` (encrypted) holds `{cookies: [...], origins: [...]}`. Cookie dicts carry `name/value/domain/path/secure/httpOnly` and, for persistent cookies, an expiry. There is no expiry field surfaced today.

## Feature 1 — Cookie-Editor JSON upload

Cookie-Editor "Export" yields a **flat array** `[{name, value, domain, path, secure, httpOnly, sameSite, expirationDate, session, ...}, …]`, not `{cookies, origins}`. And its field shapes differ from what the injection path expects (`expirationDate` float vs `expires`; `sameSite` is `"no_restriction"|"lax"|"strict"|null`).

**Backend** (`auth_realms.py`): add `_normalize_cookies(cookies: list[dict]) -> list[dict]` (sibling to `_normalize_origins`). For each cookie dict, produce a normalized cookie:
- `name`, `value` — required; skip entries without a `name`.
- `domain`, `path` (default `/`), `secure` (default False), `httpOnly` (default False) — passed through.
- `sameSite`: map `no_restriction`→`None`, `lax`→`Lax`, `strict`→`Strict`; omit when `null`/absent/unknown.
- `expires`: from `expires` if already present, else from `expirationDate` (float epoch seconds); omit for session cookies (no expiry).
- Already-normalized cookies (Playwright shape with `expires`, capitalized `sameSite`) pass through unchanged.

Apply it in `upload_session`: `realm.state_snapshot = {"cookies": _normalize_cookies(payload.cookies), "origins": _normalize_origins(payload.origins)}`. The stored shape is what the injection path (`fetch_raw` `_cookie_header`, Browserless `setCookie`) and expiry computation consume.

**Frontend** (`Logins.tsx`): the upload textarea accepts **either** a bare array (Cookie-Editor export) **or** `{cookies, origins}`. Parsing: `JSON.parse`; if the result is an array → treat as `{cookies: <array>, origins: []}`; if it's an object → require `cookies`/`origins` arrays (as today). Update the placeholder/help text to say "Paste Cookie-Editor JSON (array) or a {cookies, origins} snapshot." Malformed input → existing inline per-realm error.

## Feature 2 — Assign / change a source's auth realm post-addition

Backend: none (PATCH already supports `auth_realm_id`; `null` clears it).

**Frontend** (`SourceList.tsx`): each source row gets an inline **auth-realm selector** (reuse the realm list already loaded for the create form) showing the source's current realm — options `(public — no auth)` + each realm by name. Changing it calls `sourcesApi.update(source.id, { auth_realm_id })` (`""`→`null`) and refreshes the row. `client.ts` `sourcesApi.update` must allow `auth_realm_id` in its payload type; `Source`/`AuthRealm` types already exist in `types/index.ts`.

## Feature 3 — Expiry monitoring + block-on-expired

**Backend:**
- `schemas/auth_realm.py`: add `session_expires_at: datetime | None` to `AuthRealmResponse`.
- A new helper module `app/services/auth/session.py`: `session_expires_at(realm) -> datetime | None` = the **minimum** `expires` among `state_snapshot["cookies"]` that have one, as an aware UTC datetime; `None` when there are no cookies with an expiry. And `session_expired(realm) -> bool` = `exp is not None and exp <= now`.
- `auth_realms.py` `_response`: populate `session_expires_at` via the helper.
- `extraction.py` `trigger_extraction`: after loading the source, if it has an `auth_realm_id` whose realm `session_expired(...)` is True, return **409** with detail `"Auth session for realm '<name>' has expired — run Login (credential realms) or re-upload cookies before extracting."` Do not create an `ExtractionRun`. (Realms with `session_expires_at = None` — session-only cookies — never block; they fail loudly later if truly dead, unchanged.)

**Frontend:**
- `types/index.ts`: add `session_expires_at?: string | null` to `AuthRealm`.
- `Logins.tsx`: next to each realm's status badge, render expiry — `EXPIRED` (red) when past, else "expires in <relative>" (amber when < ~2h, else muted); nothing when `session_expires_at` is null.
- `SourceList.tsx`: when a source's selected realm is expired, show a small "auth expired" marker on the row (so it's visible before triggering).

## Module changes

- `backend/app/routes/auth_realms.py` — `_normalize_cookies`; use it in `upload_session`; `_response` sets `session_expires_at`.
- `backend/app/schemas/auth_realm.py` — `session_expires_at` on `AuthRealmResponse`.
- `backend/app/services/auth/session.py` (new, small) — `session_expires_at(realm)`, `session_expired(realm)`.
- `backend/app/routes/extraction.py` — expiry block in `trigger_extraction`.
- `frontend/src/views/Logins.tsx` — array-or-object upload parse; expiry display.
- `frontend/src/components/SourceList.tsx` — per-source realm selector; expired marker.
- `frontend/src/api/client.ts` / `types/index.ts` — `auth_realm_id` in source-update payload; `session_expires_at` on `AuthRealm`.

## Error handling

- Malformed cookie JSON (not array, not `{cookies,origins}`, or non-array fields) → clear inline error; no upload.
- A cookie dict missing `name` is skipped (not fatal); missing `value` → treated as empty string.
- Block message names the realm and the remedy; it is a 409 (not a 500) and creates no run.
- Clearing a source's realm (`auth_realm_id=null`) is allowed and reverts the source to the public path.

## Testing

**Backend (pytest, hermetic):**
- `_normalize_cookies`: Cookie-Editor entry → stored shape (sameSite `no_restriction`→`None`, `lax`→`Lax`; `expirationDate`→`expires`; session cookie → no `expires`; missing `name` skipped); already-normalized cookie passes through.
- `session_expires_at` / `session_expired`: soonest expiry chosen; None when no expiring cookies; expired when min < now, not when future.
- trigger returns 409 (and creates no run) when the source's realm session is expired; proceeds when active/future/None; unaffected for sources with no realm.
- `upload_session` stores normalized cookies (round-trip through the route with a Cookie-Editor-shaped payload).

**Frontend:** `npm run build` (type-check) green; a unit-level check of the array-vs-object upload parsing if a test harness exists, else covered by build + manual.

## Out of scope

- Background expiry jobs / proactive notifications / auto-relogin (chose block-at-trigger, not active alerting).
- File-picker upload (paste only).
- Rubrik extraction and its profile (separate spec; this UX work makes its cookie workflow easier).
