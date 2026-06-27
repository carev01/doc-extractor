# Authenticated Documentation Scraping — Design

**Date:** 2026-06-26
**Status:** Approved (brainstorming) — pending spec review
**Targets:** AvePoint, Cohesity (all docs beyond NetBackup), Rubrik

## Problem

Some vendor documentation sits behind a login wall. DocExtractor's current
pipeline assumes public pages. We need to extract complete documentation from
authenticated portals while reusing as much of the existing extraction,
sanitize, versioning, and export machinery as possible.

### Investigated auth mechanisms

| Portal | Doc URL | Auth wall |
|--------|---------|-----------|
| **Cohesity** | `docs.cohesity.com` | Hard wall — "Access to this product documentation requires authentication." Backed by MyCohesity SSO; **MFA/OTP supported**. |
| **Rubrik** | `docs.rubrik.com` | 302 → `onepassport.rubrik.com/oauth2/...` — **OAuth2/OIDC SSO** (Okta-style). Tokens likely in localStorage, not just cookies. |
| **AvePoint** | mixed | `learn.avepoint.com` behind **Azure AD B2C** (`apwebapp.b2clogin.com`); much webhelp lives openly on `cdn.avepoint.com/assets/webhelp/...` (MadCap Flare — existing profiles already handle it). |

MFA status per account is **unknown** at design time, so the design must work in
the worst case (push/SMS/email MFA, CAPTCHA) and opportunistically automate the
easy cases.

## Decisions (locked during brainstorming)

1. **Worst-case ready.** A human can always complete a login once; scripted
   username/password(+TOTP) login is an automatic optimization for portals that
   turn out to have no blocking MFA.
2. **Both login paths.** Scripted creds+TOTP first; assisted human login as
   fallback per portal.
3. **Secrets encrypted in Postgres** (master key from env / K8s Secret).
4. **Auth carried by Browserless Authenticated Profiles**, keyed by login
   domain (the "realm"). This captures the *full* auth state — cookies +
   localStorage + IndexedDB — which a cookie-only store would miss (needed for
   OIDC token storage). The realm's durable record lives in our encrypted PG;
   the live runtime state lives in a named browserless profile.

### Browserless Authenticated Profiles (confirmed capability)

- Save after login: `cdp.send('Browserless.saveProfile', { name })` → snapshots
  cookies + localStorage + IndexedDB, scoped to the API token. Returns
  `{ ok, profileId, name, cookieCount, originCount }`. Creation session expires
  after 10 min if not saved.
- Load: append `?profile=<name>` to any browser-launching request (works on
  `/function`, BrowserQL, REST, persisted sessions). Each run gets an isolated
  working copy.
- Manage via REST (list / rename / delete).

### Key architectural consequence

`?profile=` only applies to connections **we** open. Firecrawl's `/scrape`
opens its own browserless connection internally and cannot carry the profile.
**Therefore authenticated sources extract via our existing browserless
`/function` → HTML → sanitize/markdownify pipeline** (the same path used today
for Salesforce / JS-heavy profiles), *not* Firecrawl's markdown scrape. Public
sources are unchanged.

## Architecture

```
documentation_sources.auth_realm_id ──▶ auth_realm (encrypted secrets)
                                              │
        realm_manager.ensure_profile() ──────┤
            ├─ profile fresh ───────────────▶ use ?profile=<name>
            ├─ creds+TOTP present ──────────▶ scripted login (/function) ─▶ saveProfile + snapshot
            └─ else ───────────────────────▶ status=needs_login ─▶ UI assisted liveURL login ─▶ saveProfile + snapshot

   extraction (authenticated source):
        browserless /function?profile=<name> ─▶ rendered HTML ─▶ existing sanitize/markdownify ─▶ articles
        blockpage.py detects auth wall mid-run ─▶ realm status=expired, abort run, notify
```

### 1. Data model (`app/models/auth_realm.py`)

New table `auth_realm`:

| column | type | notes |
|--------|------|-------|
| `id` | UUID PK | |
| `name` | str(255) | human label |
| `login_domain` | str(512) | e.g. `docs.cohesity.com`; the realm key |
| `auth_type` | str(32) | `form` / `b2c` / `oidc` (informational; selects login script template) |
| `login_url` | str(2048) | nullable |
| `login_selectors` | JSONB | nullable per-realm selector overrides (user/pass/submit/otp fields) |
| `username` | EncryptedStr | nullable |
| `password` | EncryptedStr | nullable |
| `totp_secret` | EncryptedStr | nullable (base32 seed) |
| `browserless_profile_name` | str(255) | the `?profile=` handle (e.g. `realm-<id>`) |
| `state_snapshot` | EncryptedJSON | nullable durable copy of captured cookies/localStorage for re-seeding |
| `status` | str(32) | `active` / `needs_login` / `expired` / `login_failed` |
| `last_login_at` | datetime | nullable |
| `error_message` | str(4096) | nullable |
| `created_at` / `updated_at` | datetime | |

`documentation_sources` gains nullable `auth_realm_id` FK
(`ON DELETE SET NULL`). Null ⇒ public source, existing behavior untouched.
Add `AuthRealm` to `app/models/__init__.py` (required before `create_all`).

Alembic migration adds the table + the FK column.

### 2. Crypto (`app/core/crypto.py`)

- Fernet (via `cryptography`) keyed from `DOCEXTRACTOR_SECRET_KEY` (env / K8s
  Secret; 32-byte urlsafe base64). Fail fast on startup if a realm exists but no
  key is configured.
- SQLAlchemy `TypeDecorator`s `EncryptedStr` and `EncryptedJSON` so model fields
  encrypt/decrypt transparently; plaintext never written to disk or logs.
- New deps: `cryptography`, `pyotp`.

### 3. Auth service (`app/services/auth/`)

**`realm_manager.py`** — `ensure_profile(db, realm) -> str` returns a usable
browserless profile name, or raises `NeedsLoginError`:
1. Profile present & not `expired`/`needs_login` → return its name.
2. Else `username`+`password` present → run scripted login (TOTP computed with
   `pyotp` if `totp_secret` set), `saveProfile`, snapshot state, set `active`.
3. Else → set `needs_login`, raise `NeedsLoginError` (UI surfaces assisted
   login).
Also: `reseed_profile(db, realm)` — push `state_snapshot` into a fresh
browserless session and re-`saveProfile` when browserless has lost the profile
(e.g. container restart); `invalidate(db, realm, status)`.

**`login_scripts.py`** — per-`auth_type` `/function` ESM templates:
- `form` — generic: type into user/pass selectors, click submit, optional OTP
  field; wait for post-login navigation.
- `b2c` — Azure AD B2C two-step (email → next → password).
- `oidc` — navigate target, follow IdP redirect, fill creds, return to callback.
Selectors default per template, overridable via `realm.login_selectors`.
Each template ends with `Browserless.saveProfile` and returns
`{ ok, cookieCount, finalUrl }`.

**Assisted login** (`routes`): `POST /api/auth-realms/{id}/assisted-login`
opens a browserless interactive **liveURL** session and returns the URL; the UI
embeds it; the human logs in (handling any MFA/CAPTCHA). A follow-up
`POST .../assisted-login/complete` runs `saveProfile` on that session, snapshots
state, sets `active`.

### 4. Browserless client changes (`app/services/browserless.py`)

- `_post(...)` and the public render/expand methods accept an optional
  `profile: str | None`; when set, the `/function` endpoint URL carries
  `?profile=<name>`.
- New helpers: `save_profile(name, login_code, context)` (runs a login `/function`
  that ends in `saveProfile`), `create_live_session()` /
  `save_session_profile(session, name)` for the assisted path, and
  `seed_and_save_profile(name, state)` for re-seeding from `state_snapshot`.

### 5. Extraction integration (`app/services/firecrawl.py`)

- When a source has `auth_realm_id`: call `realm_manager.ensure_profile` at run
  start; thread the returned profile name through every browserless call for
  that run; force the browserless extraction path (skip Firecrawl `/scrape`).
- Extend `blockpage.py` with an auth-wall detector (e.g. "requires
  authentication", redirect to a login/IdP host). On detection mid-run:
  `realm_manager.invalidate(status="expired")`, abort the run with a clear
  error, notify (existing webhook/notification path).

### 6. Routes & schemas

- `app/routes/auth_realms.py` — CRUD (secrets write-only in requests, never
  returned), plus `login` (scripted), `assisted-login` + `complete`, `test`,
  `status`.
- `app/schemas/auth_realm.py` — request/response models; responses expose only
  presence flags (`has_password`, `has_totp`), never secret values.
- Source schema/route gain optional `auth_realm_id`.

### 7. Frontend

- New **Logins** view (`src/views/Logins.tsx` or equivalent): list realms with
  status badges; add/edit form (domain, login URL, auth type, optional
  username/password/TOTP, optional selector overrides); **Log in now**
  (scripted), **Assisted login** (opens embedded liveURL), **Test** buttons.
- Source form: optional realm dropdown.
- `src/api/client.ts` + `src/types/index.ts`: realm endpoints and types.

## Error handling

- Missing `DOCEXTRACTOR_SECRET_KEY` while realms exist → startup failure with a
  clear message.
- Scripted login failure → `status=login_failed`, `error_message` captured,
  surfaced in UI; never silently fall through to scraping a login page.
- Session expiry detected mid-run → run aborts cleanly, realm `expired`, user
  notified to re-login.
- Browserless profile missing at scrape time but `state_snapshot` present →
  auto `reseed_profile`; if that fails → `needs_login`.
- Secrets never logged; profile names safe to log.

## Testing

Following the repo's sync-DB + `httpx.AsyncClient` conventions:
- **Crypto:** round-trip encrypt/decrypt; `EncryptedStr`/`EncryptedJSON`
  persist ciphertext, read back plaintext; missing-key failure.
- **realm_manager:** profile-fresh short-circuit; scripted path invoked when
  creds present; `NeedsLoginError` when not; TOTP code computed when seeded;
  reseed-from-snapshot path. Browserless calls mocked.
- **blockpage:** auth-wall fixtures (Cohesity wall text, IdP redirect) detected;
  public pages not false-positived.
- **Extraction integration:** authenticated source routes through browserless
  `/function?profile=`, skips Firecrawl `/scrape`; mid-run wall → run aborts,
  realm `expired`.
- **Routes:** CRUD; secrets write-only (never echoed); status transitions.
- **login_scripts:** template renders include configured selectors and end in
  `saveProfile`.

## Out of scope

- Real-time interactive control beyond the liveURL handoff.
- Per-portal CAPTCHA solving (handled by the human in assisted login).
- Credential rotation policy / Vault (env/K8s Secret only for now).
- Changes to public-source extraction.

## Build sequence

1. Deps + crypto module + `EncryptedStr/JSON` types.
2. `auth_realm` model + `auth_realm_id` FK + `__init__.py` + Alembic migration.
3. Browserless client: `?profile=`, save/seed/liveURL helpers.
4. `realm_manager` + `login_scripts`.
5. blockpage auth-wall detector.
6. Extraction integration (route authenticated sources through profile path).
7. Routes + schemas.
8. Frontend Logins view + source realm selector.
9. Validate end-to-end against one real portal (likely AvePoint B2C first — most
   likely to lack blocking MFA) once credentials are provided.

---

## DESIGN REVISION (2026-06-26): Browserless OSS — self-managed state injection

**Discovery:** the homelab Browserless is `ghcr.io/browserless/chromium` v2.54.1 (OSS). Verified against the live instance: `/function` works, but `Browserless.saveProfile` returns "wasn't found" and BrowserQL (`/chromium/bql`) returns 404 — **Authenticated Profiles and liveURL are enterprise-only**. The original mechanism (saveProfile + `?profile=` + liveURL assisted login) cannot run here.

**Pivot (approved):** DocExtractor manages the auth state itself.
- **State** = `{cookies: [...], origins: [{origin, localStorage: [[k,v], ...]}]}`, captured via `page.cookies()` / `localStorage`, stored encrypted in `AuthRealm.state_snapshot` (already the case).
- **Replay** = inject that state into each `/function` call before navigating: `page.setCookie(...cookies)`, and for each origin `goto(origin) → localStorage.setItem(...)`, then `goto(target)`. Standard puppeteer, OSS-supported.
- **Scripted login** = unchanged except the login ESM no longer calls `saveProfile`; it captures and returns the state.
- **Assisted login** = no liveURL. The user logs in in their own browser and uploads an exported session (Playwright `storageState` JSON or cookie export); the backend normalizes and stores it as the realm's state. No Browserless needed for capture.
- `AuthRealm.browserless_profile_name` becomes a vestigial internal id (kept to avoid a migration; not used as a Browserless profile).

The realm/domain model, encryption, `realm_manager` lifecycle, auth-wall detection, and "authenticated sources extract via the `/function` path, not Firecrawl `/scrape`" all stand. Only the carrier of the session changes: our encrypted DB + per-call injection instead of a Browserless-held profile.
