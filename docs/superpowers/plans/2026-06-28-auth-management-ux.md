# Auth-management UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add UI/API controls for authenticated sources: upload Cookie-Editor JSON, assign/change a source's auth realm after creation, and monitor cookie expiry (display + block extraction when expired).

**Architecture:** Backend normalizes Cookie-Editor cookie dicts on session upload, computes a realm's soonest session expiry, exposes it on the realm response, and blocks the extraction trigger when a source's realm session is expired. Frontend accepts a bare cookie array in the upload box, shows expiry on realms, and adds a per-source realm selector.

**Tech Stack:** Backend: FastAPI, Pydantic v2, pytest (run from `backend/`, `python3`). Frontend: React/TS/Vite (run from `frontend/`; verify with `npm run build` and `npm run lint` — no JS unit-test runner in this project).

## Global Constraints

- No DB schema change. Session cookies live in `AuthRealm.state_snapshot` (`{cookies: [...], origins: [...]}`, encrypted).
- Stored cookie shape (post-normalization): `{name, value, domain, path, secure, httpOnly, sameSite?, expires?}`. `sameSite` ∈ {`Strict`,`Lax`,`None`} or omitted. `expires` is epoch seconds (number) or omitted (session cookie).
- `sameSite` mapping: `no_restriction`→`None`, `lax`→`Lax`, `strict`→`Strict` (case-insensitive for the already-capitalized Playwright values); anything else → omit.
- Expiry block is HTTP **409** and must create no `ExtractionRun`. Realms whose session has no expiring cookies (`session_expires_at = None`) never block.
- Don't change unrelated behavior: unauthenticated sources, the `{cookies, origins}` upload path, and existing realm endpoints stay working.

---

### Task 1: Backend — normalize Cookie-Editor cookies on upload

**Files:**
- Modify: `backend/app/routes/auth_realms.py` (add `_normalize_cookies`; use in `upload_session`)
- Test: `backend/tests/test_normalize_cookies.py` (new)

**Interfaces:**
- Produces: `_normalize_cookies(cookies: list[dict]) -> list[dict]` in `auth_realms.py`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_normalize_cookies.py`:

```python
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.routes.auth_realms import _normalize_cookies


def test_cookie_editor_entry_normalized():
    out = _normalize_cookies([{
        "name": "SAML_TOKEN", "value": "tok", "domain": "docs.x.com", "path": "/",
        "secure": True, "httpOnly": True, "sameSite": "lax",
        "expirationDate": 1782679307.35, "session": False,
    }])
    assert out == [{
        "name": "SAML_TOKEN", "value": "tok", "domain": "docs.x.com", "path": "/",
        "secure": True, "httpOnly": True, "sameSite": "Lax", "expires": 1782679307.35,
    }]


def test_samesite_variants_and_session_cookie():
    out = _normalize_cookies([
        {"name": "a", "value": "1", "sameSite": "no_restriction", "secure": True},
        {"name": "b", "value": "2", "sameSite": None},               # session, sameSite omitted
        {"name": "c", "value": "3", "sameSite": "Strict", "expires": 123},  # already normalized
    ])
    assert out[0]["sameSite"] == "None"
    assert "sameSite" not in out[1] and "expires" not in out[1]
    assert out[2]["sameSite"] == "Strict" and out[2]["expires"] == 123


def test_missing_name_skipped_and_value_defaults():
    out = _normalize_cookies([{"value": "x"}, {"name": "ok"}])
    assert len(out) == 1
    assert out[0] == {"name": "ok", "value": "", "domain": "", "path": "/",
                      "secure": False, "httpOnly": False}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_normalize_cookies.py -v`
Expected: FAIL — `ImportError: cannot import name '_normalize_cookies'`.

- [ ] **Step 3: Implement `_normalize_cookies`**

In `backend/app/routes/auth_realms.py`, add near `_normalize_origins`:

```python
_SAMESITE_MAP = {
    "no_restriction": "None", "lax": "Lax", "strict": "Strict",
    "none": "None", "None": "None", "Lax": "Lax", "Strict": "Strict",
}


def _normalize_cookies(cookies: list[dict]) -> list[dict]:
    """Normalise cookie dicts (Cookie-Editor export or Playwright) to the stored
    shape used for auth injection and expiry. Cookie-Editor uses ``expirationDate``
    (epoch float) and lowercase ``sameSite`` (incl. ``no_restriction``); session
    cookies have no expiry. Entries without a ``name`` are dropped.
    """
    out: list[dict] = []
    for c in cookies:
        name = c.get("name")
        if not name:
            continue
        nc = {
            "name": name,
            "value": c.get("value", ""),
            "domain": c.get("domain", ""),
            "path": c.get("path", "/"),
            "secure": bool(c.get("secure", False)),
            "httpOnly": bool(c.get("httpOnly", False)),
        }
        ss = c.get("sameSite")
        if ss in _SAMESITE_MAP:
            nc["sameSite"] = _SAMESITE_MAP[ss]
        exp = c.get("expires", c.get("expirationDate"))
        if exp is not None:
            nc["expires"] = exp
        out.append(nc)
    return out
```

- [ ] **Step 4: Use it in `upload_session`**

In `upload_session`, change the snapshot line:

```python
    realm.state_snapshot = {
        "cookies": _normalize_cookies(payload.cookies),
        "origins": normalized_origins,
    }
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_normalize_cookies.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add backend/app/routes/auth_realms.py backend/tests/test_normalize_cookies.py
git commit -m "feat(auth): normalize Cookie-Editor cookies on session upload"
```

---

### Task 2: Backend — session-expiry helper + response field

**Files:**
- Create: `backend/app/services/auth/session.py`
- Modify: `backend/app/schemas/auth_realm.py` (`AuthRealmResponse.session_expires_at`); `backend/app/routes/auth_realms.py` (`_response`)
- Test: `backend/tests/test_session_expiry.py` (new)

**Interfaces:**
- Produces: `session_expires_at(realm) -> datetime | None` and `session_expired(realm) -> bool` in `app/services/auth/session.py`. `AuthRealmResponse` gains `session_expires_at: datetime | None`.
- Consumes: `AuthRealm.state_snapshot` (`{"cookies": [{"expires": <epoch>}...]}`).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_session_expiry.py`:

```python
import os
import sys
import types
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.auth.session import session_expires_at, session_expired


def _realm(cookies):
    return types.SimpleNamespace(state_snapshot={"cookies": cookies, "origins": []})


def test_soonest_expiry_chosen():
    exp = session_expires_at(_realm([{"name": "a", "expires": 2000000000},
                                     {"name": "b", "expires": 1900000000}]))
    assert exp == datetime.fromtimestamp(1900000000, tz=timezone.utc)


def test_none_when_no_expiring_cookies():
    assert session_expires_at(_realm([{"name": "s"}])) is None
    assert session_expires_at(types.SimpleNamespace(state_snapshot=None)) is None


def test_session_expired_flag():
    past = datetime.now(timezone.utc).timestamp() - 10
    future = datetime.now(timezone.utc).timestamp() + 10000
    assert session_expired(_realm([{"name": "a", "expires": past}])) is True
    assert session_expired(_realm([{"name": "a", "expires": future}])) is False
    assert session_expired(_realm([{"name": "s"}])) is False  # no expiry → not expired
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_session_expiry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.auth.session'`.

- [ ] **Step 3: Create the helper module**

Create `backend/app/services/auth/session.py`:

```python
"""Realm session-expiry helpers, derived from the stored cookie snapshot."""

from datetime import datetime, timezone


def session_expires_at(realm) -> datetime | None:
    """Soonest expiry among the realm's stored cookies that have one, as aware
    UTC. ``None`` when the snapshot is empty or all cookies are session-only."""
    snapshot = getattr(realm, "state_snapshot", None) or {}
    expiries = [
        c["expires"]
        for c in snapshot.get("cookies", [])
        if isinstance(c.get("expires"), (int, float))
    ]
    if not expiries:
        return None
    return datetime.fromtimestamp(min(expiries), tz=timezone.utc)


def session_expired(realm) -> bool:
    """True when the realm's soonest cookie expiry is in the past."""
    exp = session_expires_at(realm)
    return exp is not None and exp <= datetime.now(timezone.utc)
```

- [ ] **Step 4: Add the response field and populate it**

In `backend/app/schemas/auth_realm.py`, add to `AuthRealmResponse` (next to `last_login_at`):

```python
    session_expires_at: datetime | None = None
```

(Confirm `from datetime import datetime` is already imported in that file — `last_login_at: datetime | None` is already there, so it is.)

In `backend/app/routes/auth_realms.py`, import the helper at the top:

```python
from app.services.auth.session import session_expires_at
```

and in `_response(...)`, add the field to the `AuthRealmResponse(...)` construction:

```python
        last_login_at=realm.last_login_at,
        session_expires_at=session_expires_at(realm),
        error_message=realm.error_message,
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_session_expiry.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Run the auth-realm route regression**

Run: `pytest tests/test_auth_realm_routes.py tests/test_auth_realm_model.py -q`
Expected: PASS (the new response field has a default; existing tests unaffected).

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/auth/session.py backend/app/schemas/auth_realm.py backend/app/routes/auth_realms.py backend/tests/test_session_expiry.py
git commit -m "feat(auth): compute + expose realm session_expires_at"
```

---

### Task 3: Backend — block extraction when the realm session is expired

**Files:**
- Modify: `backend/app/routes/extraction.py` (`trigger_extraction`)
- Test: `backend/tests/test_trigger_expired_realm.py` (new)

**Interfaces:**
- Consumes: `session_expired(realm)` (Task 2); `AuthRealm`, `DocumentationSource` models.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_trigger_expired_realm.py`. This drives the route through `httpx.AsyncClient` with the app's `get_db` overridden by a fake session that returns a source linked to an expired realm; assert 409 and that no run is enqueued.

```python
import os
import sys
import types
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
import pytest

from app.main import app
from app.core.database import get_db
from app.models.auth_realm import AuthRealm
from app.models.documentation_source import DocumentationSource
import app.routes.extraction as extraction


@pytest.mark.asyncio
async def test_trigger_blocks_on_expired_realm(monkeypatch):
    realm_id = uuid.uuid4()
    source_id = uuid.uuid4()
    source = types.SimpleNamespace(id=source_id, auth_realm_id=realm_id)
    realm = types.SimpleNamespace(
        id=realm_id, name="Rubrik Docs",
        state_snapshot={"cookies": [{"name": "t", "expires":
            datetime.now(timezone.utc).timestamp() - 10}], "origins": []},
    )

    class _Result:
        def scalar_one_or_none(self):
            return source

    class _FakeDB:
        async def execute(self, *a, **k):
            return _Result()
        async def get(self, model, pk):
            return realm if pk == realm_id else None

    async def _fake_db():
        yield _FakeDB()

    # enqueue_run must NOT be called when blocked.
    called = {"enqueue": False}
    async def _no_enqueue(*a, **k):
        called["enqueue"] = True
        raise AssertionError("enqueue_run should not run for an expired realm")
    monkeypatch.setattr(extraction, "enqueue_run", _no_enqueue)

    app.dependency_overrides[get_db] = _fake_db
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            resp = await ac.post(f"/api/extraction/trigger/{source_id}")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 409
    assert "expired" in resp.json()["detail"].lower()
    assert called["enqueue"] is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_trigger_expired_realm.py -v`
Expected: FAIL — currently returns 200/pending (no expiry check), so the 409 assertion fails.

- [ ] **Step 3: Add the expiry block**

In `backend/app/routes/extraction.py`, add the import near the other model/service imports:

```python
from app.models.auth_realm import AuthRealm
from app.services.auth.session import session_expired
```

In `trigger_extraction`, after the `if not source: raise 404` check and **before** `enqueue_run`:

```python
    if source.auth_realm_id is not None:
        realm = await db.get(AuthRealm, source.auth_realm_id)
        if realm is not None and session_expired(realm):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Auth session for realm '{realm.name}' has expired — run "
                    "Login (credential realms) or re-upload cookies before extracting."
                ),
            )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_trigger_expired_realm.py -v`
Expected: PASS.

- [ ] **Step 5: Run the extraction-route regression**

Run: `pytest tests/test_extract_auth_realm.py -q`
Expected: PASS (sources without a realm, and realms with a live/no-expiry session, are unaffected).

- [ ] **Step 6: Commit**

```bash
git add backend/app/routes/extraction.py backend/tests/test_trigger_expired_realm.py
git commit -m "feat(extract): block trigger when the source's realm session is expired"
```

---

### Task 4: Frontend — Cookie-Editor array upload + realm expiry display

**Files:**
- Modify: `frontend/src/types/index.ts` (`AuthRealm.session_expires_at`); `frontend/src/views/Logins.tsx`
- Verify: `npm run build`, `npm run lint`

**Interfaces:**
- Consumes: `AuthRealm.session_expires_at: string | null` from the backend (Task 2); `authRealmApi.uploadSession(id, {cookies, origins})` (existing).

- [ ] **Step 1: Add the type field**

In `frontend/src/types/index.ts`, add to `interface AuthRealm` (after `last_login_at`):

```ts
  session_expires_at: string | null;
```

- [ ] **Step 2: Accept a bare cookie array in the upload parser**

In `frontend/src/views/Logins.tsx`, replace the body of `handleUploadSession`'s parse/validate section (the `let parsed …` through the `if (!Array.isArray(parsed.cookies) …)` block) with:

```tsx
    let parsed: { cookies: unknown[]; origins: unknown[] };
    try {
      const raw_parsed = JSON.parse(raw);
      // Cookie-Editor exports a bare array of cookies; wrap it.
      parsed = Array.isArray(raw_parsed)
        ? { cookies: raw_parsed, origins: [] }
        : raw_parsed;
    } catch {
      setRealmError(id, 'Invalid JSON — paste a Cookie-Editor array or a {cookies, origins} object');
      return;
    }
    if (!parsed || !Array.isArray(parsed.cookies) || !Array.isArray(parsed.origins)) {
      setRealmError(id, 'Expected a Cookie-Editor cookie array, or an object with "cookies" and "origins" arrays');
      return;
    }
```

Update the upload panel's help/placeholder text (the `<p className="sub">` above the textarea, near where `sessionOpen[r.id]` renders) to mention both formats, e.g. "Paste the Cookie-Editor export (array) or a Playwright {cookies, origins} snapshot."

- [ ] **Step 3: Add an expiry indicator helper + render it**

Near the top of `Logins.tsx` (beside `statusBadge`), add:

```tsx
function expiryLabel(iso: string | null): { text: string; color: string } | null {
  if (!iso) return null;
  const ms = new Date(iso).getTime() - Date.now();
  if (ms <= 0) return { text: 'EXPIRED', color: '#e0685f' };
  const mins = Math.round(ms / 60000);
  const text = mins < 60 ? `expires in ${mins}m` : `expires in ${Math.round(mins / 60)}h`;
  return { text, color: mins < 120 ? '#eaa53d' : '#6f8087' };
}
```

In the realm row, render it next to the status badge (inside the `display:flex` header div, after `statusBadge(r.status)`):

```tsx
                {(() => {
                  const e = expiryLabel(r.session_expires_at);
                  return e ? <span className="sub" style={{ color: e.color }}>{e.text}</span> : null;
                })()}
```

- [ ] **Step 4: Build + lint**

Run: `cd frontend && npm run build && npm run lint`
Expected: build succeeds (type-check passes), lint clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/views/Logins.tsx
git commit -m "feat(ui): accept Cookie-Editor array upload + show realm session expiry"
```

---

### Task 5: Frontend — per-source auth-realm selector + expired marker

**Files:**
- Modify: `frontend/src/api/client.ts` (`updateSource` data type); `frontend/src/components/SourceList.tsx` (pass realms to `SourceItem`; add selector + marker)
- Verify: `npm run build`, `npm run lint`

**Interfaces:**
- Consumes: `updateSource(id, { auth_realm_id })` (PATCH `/sources/{id}`); `authRealms: AuthRealm[]` already loaded in `SourceList`; `DocumentationSource.auth_realm_id` and `AuthRealm.session_expires_at` (Task 4) from types.

- [ ] **Step 1: Allow `auth_realm_id` in the update payload type**

In `frontend/src/api/client.ts`, extend `updateSource`'s `data` type:

```ts
export async function updateSource(
  id: string,
  data: { name?: string; base_url?: string; platform?: string | null; refresh_profile?: boolean; url_template?: string | null; auth_realm_id?: string | null }
): Promise<DocumentationSource> {
  const res = await api.patch(`/sources/${id}`, data);
  return res.data;
}
```

- [ ] **Step 2: Pass the realm list to `SourceItem`**

In `frontend/src/components/SourceList.tsx`, pass `authRealms` (already in state) to each `SourceItem` and add it to `SourceItemProps`:

```tsx
          <SourceItem
            key={s.id}
            source={s}
            jobs={jobs}
            authRealms={authRealms}
            selected={s.id === selectedSourceId}
            onSelect={onSelectSource}
            onDelete={handleDelete}
            onSourceChanged={fetchSources}
            platformOptions={platformOptions}
            productVersion={product.version}
          />
```

```tsx
interface SourceItemProps {
  source: DocumentationSource;
  jobs: Job[];
  authRealms: AuthRealm[];
  selected: boolean;
  // ...existing props unchanged
```

(Ensure `AuthRealm` is imported in `SourceList.tsx` types import — `authRealmApi`/`AuthRealm` come from the existing imports; add `AuthRealm` to the type import from `../types` if not already present.)

- [ ] **Step 3: Render the selector + expired marker in `SourceItem`**

Inside `SourceItem` (where the row's controls render), add a realm selector and an expired marker. Add a handler:

```tsx
  const handleRealmChange = async (value: string) => {
    try {
      await updateSource(source.id, { auth_realm_id: value || null });
      onSourceChanged();
    } catch {
      /* surface via existing error UI if present; otherwise no-op */
    }
  };

  const currentRealm = authRealms.find((r) => r.id === source.auth_realm_id) || null;
  const realmExpired = !!(currentRealm?.session_expires_at
    && new Date(currentRealm.session_expires_at).getTime() <= Date.now());
```

and in the row's JSX (alongside the other per-source controls):

```tsx
      {authRealms.length > 0 && (
        <select
          value={source.auth_realm_id ?? ''}
          onChange={(e) => handleRealmChange(e.target.value)}
          title="Auth realm"
        >
          <option value="">(public — no auth)</option>
          {authRealms.map((r) => (
            <option key={r.id} value={r.id}>{r.name} [{r.login_domain}]</option>
          ))}
        </select>
      )}
      {realmExpired && (
        <span className="status-badge" style={{ backgroundColor: '#e0685f' }} title="Realm session expired">
          auth expired
        </span>
      )}
```

Ensure `updateSource` is imported from `../api/client` in `SourceList.tsx`.

- [ ] **Step 4: Build + lint**

Run: `cd frontend && npm run build && npm run lint`
Expected: build succeeds, lint clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/client.ts frontend/src/components/SourceList.tsx
git commit -m "feat(ui): per-source auth-realm selector + expired marker"
```

---

### Task 6: Verification

No code. Confirm the whole feature.

- [ ] **Step 1: Full backend suite**

Run: `cd backend && pytest -q`
Expected: PASS (previous total + the new tests).

- [ ] **Step 2: Frontend build + lint**

Run: `cd frontend && npm run build && npm run lint`
Expected: both green.

- [ ] **Step 3: Manual smoke (controller/post-merge, optional pre-merge)**

After deploy, in the UI: paste a Cookie-Editor array into a realm's upload box → realm goes ACTIVE and shows "expires in …"; assign a realm to an existing source via its new selector; trigger an extraction on a source whose realm is manually expired → expect the 409 message (not a TOC=1 run).

---

## Notes for the implementer

- Backend from `backend/`; frontend from `frontend/`. The two are separate projects.
- This plan only touches the files listed; do not refactor unrelated code.
- The heavy cookie transform is backend (Task 1, unit-tested); the frontend upload change is only "wrap a bare array as `{cookies, origins: []}`", so it needs no JS unit test beyond the build.
