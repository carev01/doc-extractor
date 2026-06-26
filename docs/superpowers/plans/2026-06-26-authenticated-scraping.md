# Authenticated Documentation Scraping — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract documentation from login-walled vendor portals (AvePoint, Cohesity, Rubrik) by reusing a captured browser auth session, integrated into the existing extraction pipeline.

**Architecture:** A new `auth_realm` table (secrets encrypted at rest in Postgres) holds credentials/TOTP and a durable auth-state snapshot, keyed by login domain. Runtime auth is carried by **Browserless Authenticated Profiles** (`?profile=<name>`), populated either by a scripted headless login or by an assisted human login over a Browserless liveURL session. Authenticated sources extract via the existing Browserless `/function` → HTML → sanitize pipeline (Firecrawl `/scrape` can't carry the profile); public sources are unchanged.

**Tech Stack:** FastAPI, SQLAlchemy (async asyncpg + sync psycopg2 in tests), Alembic, Pydantic v2, `cryptography` (Fernet), `pyotp`, httpx, Browserless `/function` + BrowserQL, React 19 + TypeScript.

**Design spec:** `docs/superpowers/specs/2026-06-26-authenticated-scraping-design.md`

## Global Constraints

- New models MUST be added to `app/models/__init__.py` before `create_all` runs.
- Backend tests use the existing patterns: model/service tests run against the async test DB (`docextractor_test`); route tests use `httpx.AsyncClient` + `ASGITransport` with `app.dependency_overrides[get_db]` (see `tests/test_jobs.py`). All external calls (Browserless, login) are mocked in tests.
- Settings use the `DOCEXTRACTOR_` env prefix; add new fields to `app/core/config.py:Settings`.
- Secrets (`username`, `password`, `totp_secret`, `state_snapshot`) MUST be encrypted at rest and MUST NEVER be returned by any API response or written to logs. API responses expose only boolean presence flags.
- Browserless token is sent as a `Bearer` header, never as a query param (`app/services/browserless.py:_post`). Profile names are safe to log.
- Routers are registered in two places: `app/routes/__init__.py` (export) and `app/main.py` (`include_router`).
- Encryption key comes from `settings.secret_key` (`DOCEXTRACTOR_SECRET_KEY`). Startup MUST fail clearly if an `auth_realm` row exists but no key is configured.

---

## File Structure

**Create:**
- `app/core/crypto.py` — Fernet helpers + `EncryptedStr` / `EncryptedJSON` SQLAlchemy `TypeDecorator`s.
- `app/models/auth_realm.py` — `AuthRealm` model + `RealmStatus` enum.
- `app/services/auth/__init__.py`
- `app/services/auth/realm_manager.py` — `ensure_profile`, `reseed_profile`, `invalidate`, `NeedsLoginError`.
- `app/services/auth/login_scripts.py` — per-`auth_type` Browserless `/function` ESM login templates.
- `app/schemas/auth_realm.py` — request/response schemas (secrets write-only).
- `app/routes/auth_realms.py` — CRUD + login/assisted-login/test endpoints.
- `frontend/src/views/Logins.tsx` — Logins management view.
- Test files (one per task, see tasks).

**Modify:**
- `app/core/config.py` — add `secret_key`.
- `app/models/source.py` — add `auth_realm_id` FK + relationship.
- `app/models/__init__.py` — import/export `AuthRealm`, `RealmStatus`.
- `app/services/browserless.py` — `profile` param on `_post`/render methods; `save_profile`, `create_live_session`, `complete_live_session`, `seed_and_save_profile` helpers.
- `app/services/blockpage.py` — `is_auth_wall` detector.
- `app/services/firecrawl.py` — wire realm → profile into the browserless extraction path; abort on auth wall.
- `app/routes/__init__.py`, `app/main.py` — register `auth_realms_router`; startup key check.
- `app/schemas/source.py`, `app/routes/sources.py` — optional `auth_realm_id`.
- `frontend/src/api/client.ts`, `frontend/src/types/index.ts`, `frontend/src/App.tsx` — realm API/types/nav.
- `requirements.txt` — `cryptography`, `pyotp`.
- A new Alembic migration under `alembic/versions/`.

---

## Task 1: Crypto module + encrypted SQLAlchemy types

**Files:**
- Modify: `requirements.txt`
- Modify: `app/core/config.py` (add `secret_key`)
- Create: `app/core/crypto.py`
- Test: `tests/test_crypto.py`

**Interfaces:**
- Produces: `app.core.crypto.encrypt(plaintext: str) -> str`, `decrypt(token: str) -> str`, `EncryptedStr` (SQLAlchemy `TypeDecorator`, `impl=Text`), `EncryptedJSON` (`TypeDecorator`, `impl=Text`, transparently JSON-encodes), `MissingKeyError(Exception)`.
- Key is read lazily from `settings.secret_key` so importing the module never fails; calling `encrypt`/`decrypt` with an empty key raises `MissingKeyError`.

- [ ] **Step 1: Add dependencies**

In `requirements.txt`, add:

```
cryptography==44.0.0
pyotp==2.9.0
```

- [ ] **Step 2: Add the settings field**

In `app/core/config.py`, inside `Settings` (near the other secret-ish fields, before `model_config`):

```python
    # Master key for encrypting credentials/sessions at rest (Fernet, urlsafe
    # base64, 32 bytes). Required only when auth_realm rows exist. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    secret_key: str = ""
```

- [ ] **Step 3: Write the failing test**

Create `tests/test_crypto.py`:

```python
import os
import sys

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core import crypto
from app.core.config import settings


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(settings, "secret_key", Fernet.generate_key().decode())
    crypto._reset_cache()  # clear the memoised cipher between tests
    yield
    crypto._reset_cache()


def test_encrypt_decrypt_roundtrip():
    assert crypto.decrypt(crypto.encrypt("hunter2")) == "hunter2"


def test_ciphertext_is_not_plaintext():
    assert "hunter2" not in crypto.encrypt("hunter2")


def test_missing_key_raises(monkeypatch):
    monkeypatch.setattr(settings, "secret_key", "")
    crypto._reset_cache()
    with pytest.raises(crypto.MissingKeyError):
        crypto.encrypt("x")
```

- [ ] **Step 4: Run test to verify it fails**

Run: `cd backend && pytest tests/test_crypto.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.crypto'`.

- [ ] **Step 5: Implement the crypto module**

Create `app/core/crypto.py`:

```python
"""Symmetric encryption for secrets stored at rest (Fernet).

The key comes from ``settings.secret_key`` (DOCEXTRACTOR_SECRET_KEY). The cipher
is built lazily and memoised so importing this module never fails when no key is
configured; only actually encrypting/decrypting requires the key.
"""

import json
from typing import Any

from cryptography.fernet import Fernet
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app.core.config import settings

_cipher: Fernet | None = None


class MissingKeyError(RuntimeError):
    """Raised when encryption is attempted without DOCEXTRACTOR_SECRET_KEY set."""


def _reset_cache() -> None:
    """Test hook — drop the memoised cipher so a new key takes effect."""
    global _cipher
    _cipher = None


def _get_cipher() -> Fernet:
    global _cipher
    if _cipher is None:
        if not settings.secret_key:
            raise MissingKeyError(
                "DOCEXTRACTOR_SECRET_KEY is required to encrypt/decrypt secrets"
            )
        _cipher = Fernet(settings.secret_key.encode())
    return _cipher


def encrypt(plaintext: str) -> str:
    return _get_cipher().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _get_cipher().decrypt(token.encode()).decode()


class EncryptedStr(TypeDecorator):
    """A string column transparently encrypted at rest."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect) -> str | None:
        return None if value is None else encrypt(value)

    def process_result_value(self, value: str | None, dialect) -> str | None:
        return None if value is None else decrypt(value)


class EncryptedJSON(TypeDecorator):
    """A JSON-serialisable column transparently encrypted at rest."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any | None, dialect) -> str | None:
        return None if value is None else encrypt(json.dumps(value))

    def process_result_value(self, value: str | None, dialect) -> Any | None:
        return None if value is None else json.loads(decrypt(value))
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd backend && pytest tests/test_crypto.py -v`
Expected: PASS (3 tests).

- [ ] **Step 7: Commit**

```bash
git add requirements.txt app/core/config.py app/core/crypto.py tests/test_crypto.py
git commit -m "feat(crypto): Fernet helpers and encrypted SQLAlchemy column types"
```

---

## Task 2: AuthRealm model, source FK, and migration

**Files:**
- Create: `app/models/auth_realm.py`
- Modify: `app/models/source.py` (add `auth_realm_id` + relationship)
- Modify: `app/models/__init__.py`
- Create: `alembic/versions/<rev>_auth_realm.py`
- Test: `tests/test_auth_realm_model.py`

**Interfaces:**
- Produces: `AuthRealm` ORM model (table `auth_realms`) with columns from the spec; `RealmStatus` str-enum (`ACTIVE="active"`, `NEEDS_LOGIN="needs_login"`, `EXPIRED="expired"`, `LOGIN_FAILED="login_failed"`). `DocumentationSource.auth_realm_id: uuid.UUID | None` and `DocumentationSource.auth_realm` relationship.

- [ ] **Step 1: Write the failing test**

Create `tests/test_auth_realm_model.py`:

```python
import os
import sys
import uuid

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core import crypto
from app.core.database import Base
from app.models import AuthRealm, RealmStatus

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def factory(monkeypatch):
    monkeypatch.setattr(settings, "secret_key", Fernet.generate_key().decode())
    crypto._reset_cache()
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()
    crypto._reset_cache()


async def test_secrets_persist_encrypted_and_read_back(factory):
    rid = uuid.uuid4()
    async with factory() as s:
        s.add(AuthRealm(
            id=rid, name="Cohesity", login_domain="docs.cohesity.com",
            auth_type="form", password="s3cret", totp_secret="JBSWY3DPEHPK3PXP",
            browserless_profile_name=f"realm-{rid}", status=RealmStatus.NEEDS_LOGIN,
        ))
        await s.commit()
    async with factory() as s:
        realm = (await s.execute(select(AuthRealm).where(AuthRealm.id == rid))).scalar_one()
        assert realm.password == "s3cret"
        assert realm.totp_secret == "JBSWY3DPEHPK3PXP"
        assert realm.status == RealmStatus.NEEDS_LOGIN
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_auth_realm_model.py -v`
Expected: FAIL — `ImportError: cannot import name 'AuthRealm'`.

- [ ] **Step 3: Create the model**

Create `app/models/auth_realm.py`:

```python
"""AuthRealm model — credentials/session for a login-walled doc domain.

One realm per login domain (e.g. docs.cohesity.com). Secrets are encrypted at
rest. Runtime auth is carried by a named Browserless profile; state_snapshot is
a durable copy used to re-seed that profile if Browserless loses it.
"""

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, Enum as SAEnum, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.crypto import EncryptedJSON, EncryptedStr
from app.core.database import Base


class RealmStatus(str, Enum):
    ACTIVE = "active"
    NEEDS_LOGIN = "needs_login"
    EXPIRED = "expired"
    LOGIN_FAILED = "login_failed"


class AuthRealm(Base):
    __tablename__ = "auth_realms"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    login_domain: Mapped[str] = mapped_column(String(512), nullable=False)
    auth_type: Mapped[str] = mapped_column(String(32), default="form", nullable=False)
    login_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    login_selectors: Mapped[dict | None] = mapped_column(EncryptedJSON, nullable=True)

    username: Mapped[str | None] = mapped_column(EncryptedStr, nullable=True)
    password: Mapped[str | None] = mapped_column(EncryptedStr, nullable=True)
    totp_secret: Mapped[str | None] = mapped_column(EncryptedStr, nullable=True)

    browserless_profile_name: Mapped[str] = mapped_column(String(255), nullable=False)
    state_snapshot: Mapped[dict | None] = mapped_column(EncryptedJSON, nullable=True)

    status: Mapped[RealmStatus] = mapped_column(
        SAEnum(RealmStatus), default=RealmStatus.NEEDS_LOGIN, nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(4096), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
```

Note: `login_selectors` is non-secret but uses `EncryptedJSON` for uniformity; that is intentional and acceptable.

- [ ] **Step 4: Add the FK on the source model**

In `app/models/source.py`, after the `job_id` column block, add:

```python
    # Optional login realm. NULL = public source (no auth). SET NULL on realm
    # delete so removing a realm just makes its sources public-only again.
    auth_realm_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auth_realms.id", ondelete="SET NULL"), nullable=True
    )
```

And in the relationships block of `DocumentationSource`, add:

```python
    auth_realm: Mapped["AuthRealm | None"] = relationship("AuthRealm")
```

- [ ] **Step 5: Register the model**

In `app/models/__init__.py`, add the import after the `source` import:

```python
from app.models.auth_realm import AuthRealm, RealmStatus
```

and add `"AuthRealm"` and `"RealmStatus"` to `__all__`.

- [ ] **Step 6: Run test to verify it passes**

Run: `cd backend && pytest tests/test_auth_realm_model.py -v`
Expected: PASS.

- [ ] **Step 7: Generate the Alembic migration**

Run: `cd backend && alembic revision --autogenerate -m "auth_realms and source.auth_realm_id"`

Open the generated file in `alembic/versions/` and verify `upgrade()` creates `auth_realms` (encrypted columns appear as `Text`) and adds `auth_realm_id` to `documentation_sources` with the `SET NULL` FK. Confirm `downgrade()` drops the column then the table.

- [ ] **Step 8: Apply and verify the migration**

Run: `cd backend && alembic upgrade head && alembic downgrade -1 && alembic upgrade head`
Expected: all three succeed with no error (round-trips cleanly).

- [ ] **Step 9: Commit**

```bash
git add app/models/auth_realm.py app/models/source.py app/models/__init__.py \
        tests/test_auth_realm_model.py alembic/versions/
git commit -m "feat(models): AuthRealm table and source.auth_realm_id FK"
```

---

## Task 3: Browserless profile + login + liveURL helpers

**Files:**
- Modify: `app/services/browserless.py`
- Test: `tests/test_browserless_auth.py`

**Interfaces:**
- Consumes: existing `BrowserlessClient._post`, `settings.browserless_url`, `settings.browserless_token`.
- Produces, on `BrowserlessClient`:
  - `_post(..., profile: str | None = None)` — appends `?profile=` to the `/function` endpoint (combined with `?timeout=` via `&`).
  - `render_html(target_url, wait_selector=None, client=None, profile=None) -> str` (profile threaded through).
  - `render(target_url, client=None, profile=None) -> dict` (profile threaded through).
  - `async def run_login(login_code: str, context: dict) -> dict` — POSTs a login `/function` (its ESM ends in `Browserless.saveProfile`); returns `{ok, cookieCount, finalUrl}`.
  - `async def seed_and_save_profile(name: str, state: dict) -> dict` — injects cookies/localStorage from `state` then saves the profile.
  - `async def create_live_session(login_url: str, timeout_ms: int) -> dict` — returns `{live_url, reconnect_endpoint}` via BrowserQL `liveURL`+`reconnect`.
  - `async def complete_live_session(reconnect_endpoint: str, profile_name: str) -> dict` — reconnects, runs `saveProfile`, returns `{ok, state}` where `state` is the captured cookies/localStorage for the snapshot.

- [ ] **Step 0: Verify the BrowserQL liveURL/reconnect shape against the homelab instance**

Before coding `create_live_session`/`complete_live_session`, confirm the live endpoint and response field names on the real instance (the BrowserQL path and `reconnect` payload field names can differ by version). Run:

```bash
curl -s -X POST "$DOCEXTRACTOR_BROWSERLESS_URL/chromium/bql?token=$DOCEXTRACTOR_BROWSERLESS_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"query":"mutation { goto(url:\"https://example.com\", waitUntil: load) { status } liveURL { liveURL } reconnect(timeout: 60000) { browserWSEndpoint } }"}'
```

Record the exact JSON paths for `liveURL` and the reconnect endpoint, and adjust the field access in Steps 6–7 to match. If the path is `/bql` rather than `/chromium/bql`, use that.

- [ ] **Step 1: Write the failing test (profile threading + login)**

Create `tests/test_browserless_auth.py`:

```python
import os
import sys
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.browserless import BrowserlessClient

pytestmark = pytest.mark.asyncio


async def test_render_html_threads_profile(monkeypatch):
    client = BrowserlessClient(url="http://bl", token="t")
    captured = {}

    async def fake_post(code, context, target_url, http_client=None, profile=None, **kw):
        captured["profile"] = profile
        return {"html": "<html></html>"}

    monkeypatch.setattr(client, "_post", fake_post)
    await client.render_html("https://docs.x.com/a", profile="realm-1")
    assert captured["profile"] == "realm-1"


async def test_run_login_returns_result(monkeypatch):
    client = BrowserlessClient(url="http://bl", token="t")

    async def fake_post(code, context, target_url, http_client=None, profile=None, **kw):
        return {"ok": True, "cookieCount": 12, "finalUrl": "https://docs.x.com/home"}

    monkeypatch.setattr(client, "_post", fake_post)
    out = await client.run_login("export default async () => {}", {"url": "https://x"})
    assert out["ok"] is True and out["cookieCount"] == 12
```

Note the test calls `_post` with keyword `profile=`; make `_post` accept it (Step 3). The `http_client`/`client` keyword in the fake must match the real signature — keep the real param name `client` and have the fake accept `client=None`. Adjust the fake's signature to `(code, context, target_url, client=None, profile=None, **kw)` to mirror the real one.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_browserless_auth.py -v`
Expected: FAIL — `render_html()`/`run_login()` reject `profile=` / don't exist.

- [ ] **Step 3: Add `profile` to `_post` and build the endpoint with query params**

In `app/services/browserless.py`, change `_post`'s signature to add `profile: str | None = None` and replace the endpoint construction:

```python
    async def _post(self, code: str, context: dict, target_url: str,
                    client: httpx.AsyncClient | None = None,
                    session_timeout_ms: int | None = None,
                    http_timeout_s: float = 120.0,
                    profile: str | None = None) -> dict:
```

Replace the two lines that build `endpoint` (currently `endpoint = f"{self.url}/function"` and the `if session_timeout_ms:` append) with:

```python
        params = {}
        if session_timeout_ms:
            params["timeout"] = str(session_timeout_ms)
        if profile:
            params["profile"] = profile
        endpoint = f"{self.url}/function"
        if params:
            from urllib.parse import urlencode
            endpoint += "?" + urlencode(params)
```

- [ ] **Step 4: Thread `profile` through `render` and `render_html`**

Update `render` and `render_html` to accept `profile: str | None = None` and pass it to `_post`:

```python
    async def render(self, target_url: str, client: httpx.AsyncClient | None = None,
                     profile: str | None = None) -> dict:
        return await self._post(
            _FUNCTION_CODE, {"url": target_url, "waitMs": self.wait_ms},
            target_url, client, profile=profile,
        )

    async def render_html(self, target_url: str, wait_selector: str | None = None,
                          client: httpx.AsyncClient | None = None,
                          profile: str | None = None) -> str:
        data = await self._post(
            _HTML_FUNCTION_CODE,
            {"url": target_url, "waitMs": self.wait_ms, "waitSelector": wait_selector},
            target_url, client, profile=profile,
        )
        return data.get("html", "")
```

- [ ] **Step 5: Add `run_login` and `seed_and_save_profile`**

Add these methods to `BrowserlessClient`:

```python
    async def run_login(self, login_code: str, context: dict) -> dict:
        """Run a login /function whose ESM ends in Browserless.saveProfile.

        The login_code must navigate, authenticate, and saveProfile(name); it
        returns {ok, cookieCount, finalUrl, state}. Uses a generous session
        timeout because IdP redirects can be slow.
        """
        timeout_ms = 120_000
        return await self._post(
            login_code, context, context.get("url", "login"),
            session_timeout_ms=timeout_ms, http_timeout_s=timeout_ms / 1000 + 30,
        )

    async def seed_and_save_profile(self, name: str, state: dict) -> dict:
        """Inject a captured cookie/localStorage snapshot into a fresh session
        and re-save it under `name` (re-seed after Browserless lost the profile)."""
        return await self._post(
            _SEED_PROFILE_CODE, {"name": name, "state": state}, f"profile:{name}",
            session_timeout_ms=60_000, http_timeout_s=90,
        )
```

Add the seed ESM near the other `_*_CODE` constants:

```python
_SEED_PROFILE_CODE = r"""
export default async function ({ page, context }) {
  const { name, state } = context;
  const cdp = await page.target().createCDPSession();
  if (state.cookies && state.cookies.length) {
    await page.setCookie(...state.cookies);
  }
  if (state.origins) {
    for (const o of state.origins) {
      await page.goto(o.origin, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await page.evaluate((items) => {
        for (const [k, v] of items) localStorage.setItem(k, v);
      }, o.localStorage || []);
    }
  }
  const res = await cdp.send('Browserless.saveProfile', { name });
  return { data: { ok: !!res.ok, cookieCount: res.cookieCount || 0 } };
}
"""
```

- [ ] **Step 6: Add `create_live_session` (BrowserQL liveURL + reconnect)**

Add (adjust field paths to match the Step 0 spike):

```python
    async def create_live_session(self, login_url: str, timeout_ms: int = 600_000) -> dict:
        """Open an interactive Browserless session at login_url and return
        {live_url, reconnect_endpoint}. The human completes login (incl. MFA)
        in live_url; reconnect_endpoint keeps the session alive for timeout_ms."""
        bql = (
            "mutation Live($url: String!) {"
            "  goto(url: $url, waitUntil: load) { status }"
            "  liveURL { liveURL }"
            "  reconnect(timeout: %d) { browserWSEndpoint }"
            "}" % timeout_ms
        )
        url = f"{self.url}/chromium/bql"
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as c:
            resp = await c.post(url, headers=headers,
                                json={"query": bql, "variables": {"url": login_url}})
            resp.raise_for_status()
            data = resp.json()["data"]
        return {
            "live_url": data["liveURL"]["liveURL"],
            "reconnect_endpoint": data["reconnect"]["browserWSEndpoint"],
        }
```

- [ ] **Step 7: Add `complete_live_session`**

```python
    async def complete_live_session(self, reconnect_endpoint: str, profile_name: str) -> dict:
        """Reconnect to a live session after the human finished logging in,
        save the auth state under profile_name, and return the state snapshot."""
        code = (
            r"export default async function ({ page }) {"
            r"  const cdp = await page.target().createCDPSession();"
            r"  const res = await cdp.send('Browserless.saveProfile', { name: '%s' });"
            r"  const cookies = await page.cookies();"
            r"  return { data: { ok: !!res.ok, cookieCount: res.cookieCount || 0, "
            r"           state: { cookies } } };"
            r"}" % profile_name
        )
        endpoint = f"{reconnect_endpoint}/function"
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0)) as c:
            resp = await c.post(endpoint, headers=headers,
                                json={"code": code, "context": {}})
            resp.raise_for_status()
            body = resp.json()
        return body.get("data", body)
```

If the Step 0 spike shows reconnect returns a full ws/http endpoint that is not directly `/function`-postable, route `complete_live_session` through a standard `?profile=` `/function` call against the same session instead; keep the return shape `{ok, cookieCount, state}` identical.

- [ ] **Step 8: Run tests**

Run: `cd backend && pytest tests/test_browserless_auth.py -v`
Expected: PASS (2 tests).

- [ ] **Step 9: Commit**

```bash
git add app/services/browserless.py tests/test_browserless_auth.py
git commit -m "feat(browserless): profile threading, login, seed, and liveURL helpers"
```

---

## Task 4: Login scripts + realm_manager

**Files:**
- Create: `app/services/auth/__init__.py` (empty)
- Create: `app/services/auth/login_scripts.py`
- Create: `app/services/auth/realm_manager.py`
- Test: `tests/test_login_scripts.py`, `tests/test_realm_manager.py`

**Interfaces:**
- Consumes: `AuthRealm`, `RealmStatus`, `browserless_client` (Task 3), `pyotp`.
- Produces:
  - `login_scripts.build_login(realm) -> tuple[str, dict]` — returns `(esm_code, context)`; context includes resolved selectors, `loginUrl`, `username`, `password`, `otp` (computed via `pyotp.TOTP(secret).now()` when `totp_secret` set), and the `profileName`.
  - `realm_manager.ensure_profile(db, realm) -> str` — returns a usable profile name or raises `NeedsLoginError`.
  - `realm_manager.invalidate(db, realm, status: RealmStatus, message: str | None = None) -> None`.
  - `realm_manager.run_scripted_login(db, realm) -> None` — runs login, stores snapshot, sets ACTIVE; on failure sets LOGIN_FAILED and raises.
  - `realm_manager.NeedsLoginError(Exception)`.

- [ ] **Step 1: Write the failing test for login_scripts**

Create `tests/test_login_scripts.py`:

```python
import os
import sys

import pyotp
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models import AuthRealm, RealmStatus
from app.services.auth import login_scripts


def _realm(**kw):
    base = dict(name="X", login_domain="docs.x.com", auth_type="form",
                login_url="https://x.com/login", username="u", password="p",
                browserless_profile_name="realm-x", status=RealmStatus.NEEDS_LOGIN)
    base.update(kw)
    return AuthRealm(**base)


def test_build_login_form_includes_creds_and_profile():
    code, ctx = login_scripts.build_login(_realm())
    assert "Browserless.saveProfile" in code
    assert ctx["username"] == "u" and ctx["password"] == "p"
    assert ctx["profileName"] == "realm-x"
    assert ctx["loginUrl"] == "https://x.com/login"


def test_build_login_computes_totp_when_seeded():
    secret = pyotp.random_base32()
    code, ctx = login_scripts.build_login(_realm(totp_secret=secret))
    assert ctx["otp"] == pyotp.TOTP(secret).now()


def test_build_login_selector_overrides_win():
    code, ctx = login_scripts.build_login(_realm(login_selectors={"username": "#email"}))
    assert ctx["selectors"]["username"] == "#email"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_login_scripts.py -v`
Expected: FAIL — module/function not found.

- [ ] **Step 3: Implement login_scripts**

Create `app/services/auth/__init__.py` (empty file).

Create `app/services/auth/login_scripts.py`:

```python
"""Per-auth-type Browserless /function login templates.

Each builder returns (esm_code, context). The ESM fills the login form, submits,
waits for navigation, then Browserless.saveProfile(profileName). Selectors have
per-auth-type defaults overridable via realm.login_selectors.
"""

import pyotp

from app.models.auth_realm import AuthRealm

_DEFAULT_SELECTORS = {
    "form": {"username": "input[type=email],input[name=username],#username",
             "password": "input[type=password]",
             "submit": "button[type=submit],input[type=submit]",
             "otp": "input[autocomplete=one-time-code],input[name=otp]"},
    "b2c": {"username": "#email,#signInName",
            "password": "#password",
            "submit": "#next,#continue,button[type=submit]",
            "otp": "#otpCode"},
    "oidc": {"username": "input[name=identifier],#username",
             "password": "input[type=password]",
             "submit": "button[type=submit]",
             "otp": "input[name=otp]"},
}

# Generic form login ESM. Types creds, optional OTP, submits, waits, saves.
_LOGIN_CODE = r"""
export default async function ({ page, context }) {
  const { loginUrl, username, password, otp, selectors, profileName, waitMs } = context;
  await page.goto(loginUrl, { waitUntil: 'networkidle2', timeout: 60000 });
  const type = async (sel, val) => {
    if (!val) return;
    const el = await page.waitForSelector(sel.split(',')[0], { timeout: 15000 }).catch(() => null)
            || await page.$(sel);
    if (el) { await el.click({ clickCount: 3 }).catch(() => {}); await el.type(val, { delay: 20 }); }
  };
  await type(selectors.username, username);
  // B2C/OIDC may need a "next" between email and password; click submit if password not yet visible.
  const pw = await page.$(selectors.password.split(',')[0]);
  if (!pw) { const n = await page.$(selectors.submit.split(',')[0]); if (n) await n.click().catch(() => {}); }
  await type(selectors.password, password);
  const submit = await page.$(selectors.submit.split(',')[0]);
  if (submit) await Promise.all([
    page.waitForNavigation({ waitUntil: 'networkidle2', timeout: 60000 }).catch(() => {}),
    submit.click(),
  ]);
  if (otp) { await type(selectors.otp, otp);
    const s2 = await page.$(selectors.submit.split(',')[0]);
    if (s2) await Promise.all([
      page.waitForNavigation({ waitUntil: 'networkidle2', timeout: 60000 }).catch(() => {}),
      s2.click(),
    ]);
  }
  await new Promise(r => setTimeout(r, waitMs || 3000));
  const cdp = await page.target().createCDPSession();
  const res = await cdp.send('Browserless.saveProfile', { name: profileName });
  const cookies = await page.cookies();
  return { data: { ok: !!res.ok, cookieCount: res.cookieCount || 0,
                   finalUrl: page.url(), state: { cookies } } };
}
"""


def build_login(realm: AuthRealm) -> tuple[str, dict]:
    defaults = _DEFAULT_SELECTORS.get(realm.auth_type, _DEFAULT_SELECTORS["form"])
    selectors = {**defaults, **(realm.login_selectors or {})}
    otp = pyotp.TOTP(realm.totp_secret).now() if realm.totp_secret else None
    context = {
        "loginUrl": realm.login_url,
        "username": realm.username,
        "password": realm.password,
        "otp": otp,
        "selectors": selectors,
        "profileName": realm.browserless_profile_name,
        "waitMs": 3000,
    }
    return _LOGIN_CODE, context
```

- [ ] **Step 4: Run login_scripts test**

Run: `cd backend && pytest tests/test_login_scripts.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Write the failing test for realm_manager**

Create `tests/test_realm_manager.py`:

```python
import os
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core import crypto
from app.core.database import Base
from app.models import AuthRealm, RealmStatus
from app.services.auth import realm_manager

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def factory(monkeypatch):
    monkeypatch.setattr(settings, "secret_key", Fernet.generate_key().decode())
    crypto._reset_cache()
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()
    crypto._reset_cache()


async def _add(factory, **kw):
    base = dict(name="X", login_domain="docs.x.com", auth_type="form",
                browserless_profile_name="realm-x", status=RealmStatus.NEEDS_LOGIN)
    base.update(kw)
    async with factory() as s:
        r = AuthRealm(**base); s.add(r); await s.commit(); await s.refresh(r)
        return r


async def test_active_realm_short_circuits(factory, monkeypatch):
    r = await _add(factory, status=RealmStatus.ACTIVE,
                   last_login_at=datetime.now(timezone.utc))
    login = AsyncMock()
    monkeypatch.setattr(realm_manager, "run_scripted_login", login)
    async with factory() as s:
        realm = await s.get(AuthRealm, r.id)
        name = await realm_manager.ensure_profile(s, realm)
    assert name == "realm-x"
    login.assert_not_awaited()


async def test_needs_login_without_creds_raises(factory):
    r = await _add(factory, status=RealmStatus.NEEDS_LOGIN)
    async with factory() as s:
        realm = await s.get(AuthRealm, r.id)
        with pytest.raises(realm_manager.NeedsLoginError):
            await realm_manager.ensure_profile(s, realm)


async def test_needs_login_with_creds_runs_scripted(factory, monkeypatch):
    r = await _add(factory, status=RealmStatus.NEEDS_LOGIN, username="u", password="p")
    async def fake_login(db, realm):
        realm.status = RealmStatus.ACTIVE
    monkeypatch.setattr(realm_manager, "run_scripted_login", fake_login)
    async with factory() as s:
        realm = await s.get(AuthRealm, r.id)
        name = await realm_manager.ensure_profile(s, realm)
    assert name == "realm-x"


async def test_run_scripted_login_stores_snapshot(factory, monkeypatch):
    r = await _add(factory, status=RealmStatus.NEEDS_LOGIN, username="u", password="p")
    fake_client = AsyncMock()
    fake_client.run_login.return_value = {
        "ok": True, "cookieCount": 3, "finalUrl": "https://docs.x.com/",
        "state": {"cookies": [{"name": "sid", "value": "abc"}]},
    }
    monkeypatch.setattr(realm_manager, "browserless_client", fake_client)
    async with factory() as s:
        realm = await s.get(AuthRealm, r.id)
        await realm_manager.run_scripted_login(s, realm)
        await s.commit()
        assert realm.status == RealmStatus.ACTIVE
        assert realm.state_snapshot["cookies"][0]["name"] == "sid"
```

- [ ] **Step 6: Run realm_manager test to verify it fails**

Run: `cd backend && pytest tests/test_realm_manager.py -v`
Expected: FAIL — module/functions not found.

- [ ] **Step 7: Implement realm_manager**

Create `app/services/auth/realm_manager.py`:

```python
"""Realm session lifecycle — ensure a usable Browserless profile exists."""

import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth_realm import AuthRealm, RealmStatus
from app.services.auth import login_scripts
from app.services.browserless import BrowserlessError, browserless_client

logger = logging.getLogger(__name__)


class NeedsLoginError(Exception):
    """A realm has no usable session and cannot be logged in without a human."""


async def ensure_profile(db: AsyncSession, realm: AuthRealm) -> str:
    """Return a usable Browserless profile name for `realm`, logging in via
    scripted creds if needed. Raises NeedsLoginError when a human is required."""
    if realm.status == RealmStatus.ACTIVE:
        return realm.browserless_profile_name
    if realm.username and realm.password:
        await run_scripted_login(db, realm)
        return realm.browserless_profile_name
    await invalidate(db, realm, RealmStatus.NEEDS_LOGIN,
                     "No stored credentials; assisted login required")
    raise NeedsLoginError(f"Realm {realm.id} needs an assisted login")


async def run_scripted_login(db: AsyncSession, realm: AuthRealm) -> None:
    """Run a headless scripted login; persist snapshot + ACTIVE, or LOGIN_FAILED."""
    code, context = login_scripts.build_login(realm)
    try:
        result = await browserless_client.run_login(code, context)
    except BrowserlessError as exc:
        await invalidate(db, realm, RealmStatus.LOGIN_FAILED, str(exc))
        raise NeedsLoginError(f"Scripted login failed for realm {realm.id}") from exc
    if not result.get("ok"):
        await invalidate(db, realm, RealmStatus.LOGIN_FAILED,
                         f"Login did not complete (finalUrl={result.get('finalUrl')})")
        raise NeedsLoginError(f"Scripted login did not complete for realm {realm.id}")
    realm.state_snapshot = result.get("state")
    realm.status = RealmStatus.ACTIVE
    realm.last_login_at = datetime.now(timezone.utc)
    realm.error_message = None
    await db.flush()


async def invalidate(db: AsyncSession, realm: AuthRealm, status: RealmStatus,
                     message: str | None = None) -> None:
    realm.status = status
    realm.error_message = message
    await db.flush()
```

- [ ] **Step 8: Run realm_manager tests**

Run: `cd backend && pytest tests/test_realm_manager.py -v`
Expected: PASS (4 tests).

- [ ] **Step 9: Commit**

```bash
git add app/services/auth/ tests/test_login_scripts.py tests/test_realm_manager.py
git commit -m "feat(auth): login script templates and realm session manager"
```

---

## Task 5: Auth-wall detector

**Files:**
- Modify: `app/services/blockpage.py`
- Test: `tests/test_auth_wall.py`

**Interfaces:**
- Produces: `app.services.blockpage.is_auth_wall(text: str, final_url: str | None = None, login_domain: str | None = None) -> bool`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_auth_wall.py`:

```python
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.blockpage import is_auth_wall


def test_cohesity_wall_text_detected():
    assert is_auth_wall("Access to this product documentation requires authentication. Please sign in to continue.")


def test_idp_redirect_detected():
    assert is_auth_wall("redirecting...", final_url="https://onepassport.rubrik.com/oauth2/v1/authorize?x=1")


def test_real_doc_about_authentication_not_flagged():
    body = ("This guide explains how to configure single sign-on. " * 80)
    assert not is_auth_wall(body, final_url="https://docs.rubrik.com/en-us/sso.html")


def test_b2c_login_host_redirect_detected():
    assert is_auth_wall("", final_url="https://apwebapp.b2clogin.com/whatever")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_auth_wall.py -v`
Expected: FAIL — `ImportError: cannot import name 'is_auth_wall'`.

- [ ] **Step 3: Implement `is_auth_wall`**

Append to `app/services/blockpage.py`:

```python
# Login-host fragments: if the *final* URL lands on one of these, the scrape was
# bounced to an identity provider / login page rather than the docs content.
_LOGIN_HOST_MARKERS = (
    "login", "signin", "sign-in", "oauth2", "/authorize", "b2clogin.com",
    "onepassport", "auth0.com", "okta.com", "/saml", "accounts.google.com",
)

# Auth-wall phrases. Short-page-gated like _SHORT_MARKERS so a real article that
# documents authentication isn't misflagged.
_AUTH_WALL_MARKERS = (
    "requires authentication",
    "please sign in to continue",
    "you must be logged in to view",
    "session has expired",
)


def is_auth_wall(text: str, final_url: str | None = None,
                 login_domain: str | None = None) -> bool:
    """Return True if a scrape was bounced to a login wall / IdP rather than
    returning documentation content."""
    if final_url:
        low_url = final_url.lower()
        if any(m in low_url for m in _LOGIN_HOST_MARKERS):
            return True
    if text:
        low = text.lower()
        if len(text.strip()) <= _SHORT_PAGE_LIMIT and any(m in low for m in _AUTH_WALL_MARKERS):
            return True
    return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_auth_wall.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/blockpage.py tests/test_auth_wall.py
git commit -m "feat(blockpage): detect auth-wall / IdP-redirect pages"
```

---

## Task 6: Extraction integration

**Files:**
- Modify: `app/services/firecrawl.py`
- Test: `tests/test_extract_auth_realm.py`

**Interfaces:**
- Consumes: `realm_manager.ensure_profile`, `realm_manager.NeedsLoginError`, `realm_manager.invalidate`, `is_auth_wall`, `browserless_client.render_html(..., profile=...)`.
- Behavior: when `source.auth_realm_id` is set, `extract_source` resolves a profile name once at the start (raising/aborting the run on `NeedsLoginError`), forces the Browserless extraction path, threads the profile into Browserless render calls, and aborts the run (marking the realm `expired`) if an auth wall is detected mid-run.

- [ ] **Step 1: Write the failing test**

Create `tests/test_extract_auth_realm.py`:

```python
import os
import sys
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core import crypto
from app.core.database import Base
from app.models import Vendor, Product, DocumentationSource, AuthRealm, RealmStatus
from app.models.extraction_run import RunStatus
from app.services.firecrawl import firecrawl_service
from app.services.auth import realm_manager

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def factory(monkeypatch):
    monkeypatch.setattr(settings, "secret_key", Fernet.generate_key().decode())
    crypto._reset_cache()
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()
    crypto._reset_cache()


async def _source_with_realm(factory, realm_status):
    async with factory() as s:
        v = Vendor(name="Cohesity"); s.add(v); await s.flush()
        p = Product(name="DataProtect", vendor_id=v.id); s.add(p); await s.flush()
        r = AuthRealm(name="Cohesity", login_domain="docs.cohesity.com",
                      browserless_profile_name="realm-c", status=realm_status)
        s.add(r); await s.flush()
        src = DocumentationSource(name="DP Docs", base_url="https://docs.cohesity.com/dp",
                                  product_id=p.id, platform="generic", auth_realm_id=r.id)
        s.add(src); await s.commit(); await s.refresh(src)
        return src.id


async def test_needs_login_aborts_run(factory):
    src_id = await _source_with_realm(factory, RealmStatus.NEEDS_LOGIN)
    async with factory() as s:
        run = await firecrawl_service.extract_source(s, src_id)
        await s.commit()
    assert run.status == RunStatus.FAILED
    assert "login" in (run.error_message or "").lower()
```

This test relies on a realm with no creds raising `NeedsLoginError`, which `extract_source` must catch and convert to a failed run before any scraping.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_extract_auth_realm.py -v`
Expected: FAIL — run is not marked FAILED with a login message (current code ignores the realm).

- [ ] **Step 3: Resolve the profile at the start of `extract_source`**

In `app/services/firecrawl.py`, add imports near the top (with the other `app.services` imports):

```python
from app.services.auth import realm_manager
from app.services.auth.realm_manager import NeedsLoginError
```

In `extract_source`, immediately after the block that loads `source` and the run row and sets `run.status = RunStatus.RUNNING`, insert:

```python
        # Authenticated source: resolve a Browserless profile up front. If the
        # realm needs a human login, fail the run cleanly instead of scraping a
        # login page.
        auth_profile: str | None = None
        if source.auth_realm_id is not None:
            realm = await db.get(AuthRealm, source.auth_realm_id)
            try:
                auth_profile = await realm_manager.ensure_profile(db, realm)
            except NeedsLoginError as exc:
                run.status = RunStatus.FAILED
                run.error_message = f"Authenticated source needs login: {exc}"
                await db.commit()
                return run
```

Add `AuthRealm` to the existing `app.models` imports in `firecrawl.py` (find the line importing `DocumentationSource` and add `AuthRealm`).

- [ ] **Step 4: Thread the profile into the Browserless content path**

`extract_source` must force the Browserless path and pass `auth_profile` when the source is authenticated. Locate the content-scraping branch (around `app/services/firecrawl.py:1465`, `if getattr(profile, "render_engine", None) == "browserless":` and the `_scrape_via_browserless` call near line 1472). Change the condition so an authenticated source always takes the Browserless branch:

```python
            if auth_profile is not None or getattr(profile, "render_engine", None) == "browserless":
```

Add a `profile` parameter to `_scrape_via_browserless` (signature around line 856) defaulting to `None`, and forward it to each `browserless_client.render(...)` / `render_html(...)` call inside that method by adding `profile=profile`. Update the call site (around line 1472) to pass `profile=auth_profile`.

- [ ] **Step 5: Abort on auth wall mid-run**

Inside `_scrape_via_browserless`, after obtaining the rendered HTML/text for a page and before storing it, add (using the already-imported `is_block_page`; add `is_auth_wall` to that import):

```python
        from app.services.blockpage import is_auth_wall
        if is_auth_wall(content_text or content_html or "", final_url=url):
            raise NeedsLoginError(f"Auth wall hit at {url}; session expired")
```

(Use the variable names already present in `_scrape_via_browserless` for the rendered text/html and the page URL.) Then in `extract_source`, wrap the content-scraping call so a `NeedsLoginError` raised mid-run marks the realm expired and fails the run:

```python
            try:
                await self._scrape_via_browserless(..., profile=auth_profile)
            except NeedsLoginError as exc:
                if source.auth_realm_id is not None:
                    realm = await db.get(AuthRealm, source.auth_realm_id)
                    await realm_manager.invalidate(db, realm, RealmStatus.EXPIRED, str(exc))
                run.status = RunStatus.FAILED
                run.error_message = f"Session expired mid-run: {exc}"
                await db.commit()
                return run
```

Keep the existing positional arguments to `_scrape_via_browserless`; only add the `profile=` keyword. Import `RealmStatus` alongside `AuthRealm` in `firecrawl.py`.

- [ ] **Step 6: Run the test to verify it passes**

Run: `cd backend && pytest tests/test_extract_auth_realm.py -v`
Expected: PASS.

- [ ] **Step 7: Run the broader extraction suite for regressions**

Run: `cd backend && pytest tests/ -k "extract or firecrawl or browserless" -q`
Expected: PASS (no regressions in existing extraction tests).

- [ ] **Step 8: Commit**

```bash
git add app/services/firecrawl.py tests/test_extract_auth_realm.py
git commit -m "feat(extraction): route authenticated sources through Browserless profile; abort on auth wall"
```

---

## Task 7: Routes, schemas, startup key check, source field

**Files:**
- Create: `app/schemas/auth_realm.py`
- Create: `app/routes/auth_realms.py`
- Modify: `app/routes/__init__.py`, `app/main.py`
- Modify: `app/schemas/source.py`, `app/routes/sources.py`
- Test: `tests/test_auth_realm_routes.py`

**Interfaces:**
- Produces: `auth_realms_router` mounted at `/api/auth-realms` with: `POST /` (create), `GET /` (list), `GET /{id}`, `PATCH /{id}`, `DELETE /{id}`, `POST /{id}/login` (scripted), `POST /{id}/assisted-login` (returns `{live_url}` + persists the reconnect endpoint), `POST /{id}/assisted-login/complete`, `POST /{id}/test`. Responses use `AuthRealmResponse` with `has_password`/`has_totp` booleans and never secret values. `SourceResponse`/create/update gain optional `auth_realm_id`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_auth_realm_routes.py` (mirrors the `tests/test_jobs.py` client fixture; sets `secret_key`):

```python
import os
import sys

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core import crypto
from app.core.database import Base, get_db
from app.main import app

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setattr(settings, "secret_key", Fernet.generate_key().decode())
    crypto._reset_cache()
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()
    crypto._reset_cache()


async def test_create_realm_never_returns_secrets(client):
    resp = await client.post("/api/auth-realms", json={
        "name": "Cohesity", "login_domain": "docs.cohesity.com", "auth_type": "form",
        "login_url": "https://my.cohesity.com/login",
        "username": "u@x.com", "password": "p", "totp_secret": "JBSWY3DPEHPK3PXP",
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "password" not in body and "totp_secret" not in body and "state_snapshot" not in body
    assert body["has_password"] is True and body["has_totp"] is True
    assert body["status"] == "needs_login"


async def test_list_and_get(client):
    await client.post("/api/auth-realms", json={
        "name": "Rubrik", "login_domain": "docs.rubrik.com", "auth_type": "oidc"})
    lst = (await client.get("/api/auth-realms")).json()
    assert len(lst) == 1 and lst[0]["has_password"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_auth_realm_routes.py -v`
Expected: FAIL — 404 (router not mounted).

- [ ] **Step 3: Create the schemas**

Create `app/schemas/auth_realm.py`:

```python
"""AuthRealm request/response schemas. Secrets are write-only."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class AuthRealmCreate(BaseModel):
    name: str
    login_domain: str
    auth_type: Literal["form", "b2c", "oidc"] = "form"
    login_url: str | None = None
    login_selectors: dict | None = None
    username: str | None = None
    password: str | None = None
    totp_secret: str | None = None


class AuthRealmUpdate(BaseModel):
    name: str | None = None
    login_url: str | None = None
    login_selectors: dict | None = None
    username: str | None = None
    password: str | None = None
    totp_secret: str | None = None


class AuthRealmResponse(BaseModel):
    id: uuid.UUID
    name: str
    login_domain: str
    auth_type: str
    login_url: str | None
    status: str
    has_username: bool
    has_password: bool
    has_totp: bool
    last_login_at: datetime | None
    error_message: str | None


class LiveSessionResponse(BaseModel):
    live_url: str
```

- [ ] **Step 4: Create the router**

Create `app/routes/auth_realms.py`:

```python
"""Auth realm routes — manage login-walled doc credentials/sessions."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.auth_realm import AuthRealm, RealmStatus
from app.schemas.auth_realm import (
    AuthRealmCreate, AuthRealmUpdate, AuthRealmResponse, LiveSessionResponse,
)
from app.services.auth import realm_manager
from app.services.auth.realm_manager import NeedsLoginError
from app.services.browserless import browserless_client

router = APIRouter(prefix="/api/auth-realms", tags=["auth-realms"])

# Reconnect endpoints for in-flight assisted logins, keyed by realm id. In-memory
# is fine: an assisted login is a single short-lived interactive flow.
_LIVE_SESSIONS: dict[uuid.UUID, str] = {}


def _response(r: AuthRealm) -> AuthRealmResponse:
    return AuthRealmResponse(
        id=r.id, name=r.name, login_domain=r.login_domain, auth_type=r.auth_type,
        login_url=r.login_url, status=r.status.value if hasattr(r.status, "value") else r.status,
        has_username=bool(r.username), has_password=bool(r.password),
        has_totp=bool(r.totp_secret), last_login_at=r.last_login_at,
        error_message=r.error_message,
    )


async def _get(db: AsyncSession, realm_id: uuid.UUID) -> AuthRealm:
    realm = await db.get(AuthRealm, realm_id)
    if realm is None:
        raise HTTPException(404, "Auth realm not found")
    return realm


@router.post("", status_code=201, response_model=AuthRealmResponse)
async def create_realm(payload: AuthRealmCreate, db: AsyncSession = Depends(get_db)):
    realm = AuthRealm(
        name=payload.name, login_domain=payload.login_domain, auth_type=payload.auth_type,
        login_url=payload.login_url, login_selectors=payload.login_selectors,
        username=payload.username, password=payload.password, totp_secret=payload.totp_secret,
        browserless_profile_name=f"realm-{uuid.uuid4()}", status=RealmStatus.NEEDS_LOGIN,
    )
    db.add(realm)
    await db.commit()
    await db.refresh(realm)
    return _response(realm)


@router.get("", response_model=list[AuthRealmResponse])
async def list_realms(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(AuthRealm).order_by(AuthRealm.name))).scalars().all()
    return [_response(r) for r in rows]


@router.get("/{realm_id}", response_model=AuthRealmResponse)
async def get_realm(realm_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return _response(await _get(db, realm_id))


@router.patch("/{realm_id}", response_model=AuthRealmResponse)
async def update_realm(realm_id: uuid.UUID, payload: AuthRealmUpdate,
                       db: AsyncSession = Depends(get_db)):
    realm = await _get(db, realm_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(realm, field, value)
    await db.commit()
    await db.refresh(realm)
    return _response(realm)


@router.delete("/{realm_id}", status_code=204)
async def delete_realm(realm_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    realm = await _get(db, realm_id)
    await db.delete(realm)
    await db.commit()


@router.post("/{realm_id}/login", response_model=AuthRealmResponse)
async def scripted_login(realm_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    realm = await _get(db, realm_id)
    if not (realm.username and realm.password):
        raise HTTPException(400, "Realm has no stored credentials; use assisted login")
    try:
        await realm_manager.run_scripted_login(db, realm)
    except NeedsLoginError as exc:
        await db.commit()
        raise HTTPException(409, str(exc))
    await db.commit()
    await db.refresh(realm)
    return _response(realm)


@router.post("/{realm_id}/assisted-login", response_model=LiveSessionResponse)
async def assisted_login(realm_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    realm = await _get(db, realm_id)
    if not realm.login_url:
        raise HTTPException(400, "Realm has no login_url")
    session = await browserless_client.create_live_session(realm.login_url)
    _LIVE_SESSIONS[realm.id] = session["reconnect_endpoint"]
    return LiveSessionResponse(live_url=session["live_url"])


@router.post("/{realm_id}/assisted-login/complete", response_model=AuthRealmResponse)
async def assisted_login_complete(realm_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    realm = await _get(db, realm_id)
    endpoint = _LIVE_SESSIONS.pop(realm.id, None)
    if endpoint is None:
        raise HTTPException(409, "No assisted-login session in progress")
    result = await browserless_client.complete_live_session(
        endpoint, realm.browserless_profile_name)
    if not result.get("ok"):
        await realm_manager.invalidate(db, realm, RealmStatus.LOGIN_FAILED,
                                       "Assisted login did not capture a session")
        await db.commit()
        raise HTTPException(409, "Assisted login did not capture a session")
    realm.state_snapshot = result.get("state")
    realm.status = RealmStatus.ACTIVE
    realm.error_message = None
    await db.commit()
    await db.refresh(realm)
    return _response(realm)


@router.post("/{realm_id}/test", response_model=AuthRealmResponse)
async def test_realm(realm_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Fetch the login_domain root through the realm's profile and verify it is
    not an auth wall."""
    from app.services.blockpage import is_auth_wall
    realm = await _get(db, realm_id)
    try:
        name = await realm_manager.ensure_profile(db, realm)
    except NeedsLoginError as exc:
        await db.commit()
        raise HTTPException(409, str(exc))
    html = await browserless_client.render_html(
        f"https://{realm.login_domain}/", profile=name)
    if is_auth_wall(html, final_url=f"https://{realm.login_domain}/"):
        await realm_manager.invalidate(db, realm, RealmStatus.EXPIRED, "Test hit auth wall")
    await db.commit()
    await db.refresh(realm)
    return _response(realm)
```

- [ ] **Step 5: Register the router**

In `app/routes/__init__.py`, add `from app.routes.auth_realms import router as auth_realms_router` and add `"auth_realms_router"` to `__all__`.

In `app/main.py`, add `auth_realms_router` to the `from app.routes import (...)` block and add `app.include_router(auth_realms_router)` with the other `include_router` calls.

- [ ] **Step 6: Add the startup key check**

In `app/main.py`, inside the `lifespan` startup (after `create_all`), add:

```python
    # Fail fast if encrypted realms exist but no key is configured.
    from sqlalchemy import select as _select
    from app.models.auth_realm import AuthRealm as _AuthRealm
    async with engine.begin() as conn:
        has_realm = (await conn.execute(_select(_AuthRealm.id).limit(1))).first()
    if has_realm and not settings.secret_key:
        raise RuntimeError(
            "auth_realm rows exist but DOCEXTRACTOR_SECRET_KEY is not set"
        )
```

- [ ] **Step 7: Add `auth_realm_id` to the source schema/route**

In `app/schemas/source.py`, add `auth_realm_id: uuid.UUID | None = None` to the source create, update, and response models (match the file's existing model names; ensure `import uuid` is present). In `app/routes/sources.py`, ensure create/update persist `auth_realm_id` and the response includes it (follow how `job_id` is already handled there).

- [ ] **Step 8: Run the route tests**

Run: `cd backend && pytest tests/test_auth_realm_routes.py -v`
Expected: PASS (2 tests).

- [ ] **Step 9: Run the full backend suite**

Run: `cd backend && pytest -q`
Expected: PASS (no regressions).

- [ ] **Step 10: Commit**

```bash
git add app/schemas/auth_realm.py app/routes/auth_realms.py app/routes/__init__.py \
        app/main.py app/schemas/source.py app/routes/sources.py tests/test_auth_realm_routes.py
git commit -m "feat(api): auth-realm CRUD, scripted/assisted login, test endpoint, source field"
```

---

## Task 8: Frontend Logins view + source realm selector

**Files:**
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/api/client.ts`
- Create: `frontend/src/views/Logins.tsx`
- Modify: `frontend/src/App.tsx` (nav + view), and the source form component (realm dropdown)
- (No JS unit tests in this repo; validation is type-check + build + lint.)

**Interfaces:**
- Consumes: `/api/auth-realms` endpoints from Task 7.
- Produces: an `AuthRealm` TS type, `authRealmApi` client functions, a `Logins` view, and an optional realm `<select>` on the source form.

- [ ] **Step 1: Add the type**

In `frontend/src/types/index.ts`, add:

```typescript
export interface AuthRealm {
  id: string;
  name: string;
  login_domain: string;
  auth_type: 'form' | 'b2c' | 'oidc';
  login_url: string | null;
  status: 'active' | 'needs_login' | 'expired' | 'login_failed';
  has_username: boolean;
  has_password: boolean;
  has_totp: boolean;
  last_login_at: string | null;
  error_message: string | null;
}

export interface AuthRealmCreate {
  name: string;
  login_domain: string;
  auth_type: 'form' | 'b2c' | 'oidc';
  login_url?: string | null;
  username?: string | null;
  password?: string | null;
  totp_secret?: string | null;
}
```

Add an optional `auth_realm_id?: string | null;` to the existing `Source`-related interfaces (the source response and the create/update payload types).

- [ ] **Step 2: Add the API client functions**

In `frontend/src/api/client.ts`, following the existing axios pattern, add:

```typescript
export const authRealmApi = {
  list: () => api.get<AuthRealm[]>('/api/auth-realms').then((r) => r.data),
  create: (data: AuthRealmCreate) =>
    api.post<AuthRealm>('/api/auth-realms', data).then((r) => r.data),
  update: (id: string, data: Partial<AuthRealmCreate>) =>
    api.patch<AuthRealm>(`/api/auth-realms/${id}`, data).then((r) => r.data),
  remove: (id: string) => api.delete(`/api/auth-realms/${id}`),
  login: (id: string) =>
    api.post<AuthRealm>(`/api/auth-realms/${id}/login`).then((r) => r.data),
  assistedLogin: (id: string) =>
    api.post<{ live_url: string }>(`/api/auth-realms/${id}/assisted-login`).then((r) => r.data),
  assistedLoginComplete: (id: string) =>
    api.post<AuthRealm>(`/api/auth-realms/${id}/assisted-login/complete`).then((r) => r.data),
  test: (id: string) =>
    api.post<AuthRealm>(`/api/auth-realms/${id}/test`).then((r) => r.data),
};
```

Import `AuthRealm`, `AuthRealmCreate` at the top from `../types`.

- [ ] **Step 3: Create the Logins view**

Create `frontend/src/views/Logins.tsx` — a list of realms with a status badge, an add form (name, login_domain, auth_type, login_url, username, password, totp_secret), and per-realm **Log in**, **Assisted login**, **Test**, **Delete** buttons. Assisted login opens `live_url` in a new tab, then on a "I've finished logging in" button calls `assistedLoginComplete`:

```tsx
import { useEffect, useState } from 'react';
import { authRealmApi } from '../api/client';
import type { AuthRealm, AuthRealmCreate } from '../types';

const EMPTY: AuthRealmCreate = {
  name: '', login_domain: '', auth_type: 'form',
  login_url: '', username: '', password: '', totp_secret: '',
};

export function Logins() {
  const [realms, setRealms] = useState<AuthRealm[]>([]);
  const [form, setForm] = useState<AuthRealmCreate>(EMPTY);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = () => authRealmApi.list().then(setRealms);
  useEffect(() => { refresh(); }, []);

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    await authRealmApi.create(form);
    setForm(EMPTY);
    refresh();
  };

  const assisted = async (id: string) => {
    const { live_url } = await authRealmApi.assistedLogin(id);
    window.open(live_url, '_blank', 'noopener');
    setBusy(id);
  };

  const finishAssisted = async (id: string) => {
    await authRealmApi.assistedLoginComplete(id);
    setBusy(null);
    refresh();
  };

  return (
    <div className="logins-view">
      <h2>Logins</h2>
      <form onSubmit={create} className="realm-form">
        <input placeholder="Name" value={form.name}
               onChange={(e) => setForm({ ...form, name: e.target.value })} required />
        <input placeholder="Login domain (docs.x.com)" value={form.login_domain}
               onChange={(e) => setForm({ ...form, login_domain: e.target.value })} required />
        <select value={form.auth_type}
                onChange={(e) => setForm({ ...form, auth_type: e.target.value as AuthRealmCreate['auth_type'] })}>
          <option value="form">form</option>
          <option value="b2c">b2c</option>
          <option value="oidc">oidc</option>
        </select>
        <input placeholder="Login URL" value={form.login_url ?? ''}
               onChange={(e) => setForm({ ...form, login_url: e.target.value })} />
        <input placeholder="Username" value={form.username ?? ''}
               onChange={(e) => setForm({ ...form, username: e.target.value })} />
        <input type="password" placeholder="Password" value={form.password ?? ''}
               onChange={(e) => setForm({ ...form, password: e.target.value })} />
        <input placeholder="TOTP secret (optional)" value={form.totp_secret ?? ''}
               onChange={(e) => setForm({ ...form, totp_secret: e.target.value })} />
        <button type="submit">Add realm</button>
      </form>

      <ul className="realm-list">
        {realms.map((r) => (
          <li key={r.id}>
            <span className={`badge status-${r.status}`}>{r.status}</span>
            <strong>{r.name}</strong> <code>{r.login_domain}</code>
            {r.error_message && <em className="err"> {r.error_message}</em>}
            <div className="realm-actions">
              {r.has_password && (
                <button onClick={async () => { await authRealmApi.login(r.id); refresh(); }}>Log in</button>
              )}
              <button onClick={() => assisted(r.id)}>Assisted login</button>
              {busy === r.id && (
                <button onClick={() => finishAssisted(r.id)}>I&apos;ve finished logging in</button>
              )}
              <button onClick={async () => { await authRealmApi.test(r.id); refresh(); }}>Test</button>
              <button onClick={async () => { await authRealmApi.remove(r.id); refresh(); }}>Delete</button>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
```

- [ ] **Step 4: Wire the view into navigation**

In `frontend/src/App.tsx`, add a `'logins'` value to the view state union and a nav entry/button rendering `<Logins />` (follow the existing `vendors`/`sources`/`export` view-switch pattern). Import `{ Logins }` from `./views/Logins`.

- [ ] **Step 5: Add the realm selector to the source form**

In the source create/edit form component, add an optional realm `<select>` populated from `authRealmApi.list()` (blank option = public). Bind it to the source payload's `auth_realm_id`.

- [ ] **Step 6: Type-check, build, lint**

Run: `cd frontend && npm run build && npm run lint`
Expected: build succeeds, lint passes.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/api/client.ts \
        frontend/src/views/Logins.tsx frontend/src/App.tsx
git commit -m "feat(frontend): Logins view and source realm selector"
```

---

## Task 9: End-to-end validation against a real portal

**Files:** none (manual validation; record findings).

This task is gated on real credentials. Do it once the credentials are supplied. Start with **AvePoint Learn** (`learn.avepoint.com`, Azure AD B2C) — it is the most likely to lack blocking MFA, so the scripted path can be proven first.

- [ ] **Step 1:** Set `DOCEXTRACTOR_SECRET_KEY` in the deployment env (generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
- [ ] **Step 2:** Create the realm via the Logins UI (`auth_type=b2c`, login_url = the AvePoint Learn login URL, username/password).
- [ ] **Step 3:** Click **Log in** (scripted). Confirm status → `active`. If it fails (push/SMS MFA / CAPTCHA), use **Assisted login** and complete it in the live tab.
- [ ] **Step 4:** Create a source under that realm pointing at an AvePoint Learn doc tree; trigger an extraction.
- [ ] **Step 5:** Verify articles are stored (not login pages) and the run reports COMPLETED. Spot-check one article's content.
- [ ] **Step 6:** Repeat for Cohesity (`docs.cohesity.com`, expect MFA → assisted login) and Rubrik (`docs.rubrik.com`, `oidc`). Record per-portal which login path worked and any selector overrides needed in a short note appended to the design spec.

---

## Self-Review Notes

- **Spec coverage:** data model (T2), crypto (T1), auth service `realm_manager`+`login_scripts` (T4), browserless profile/liveURL helpers (T3), blockpage auth-wall detector (T5), extraction integration (T6), routes/schemas + startup key check + source field (T7), frontend (T8), validation incl. AvePoint-first (T9). All spec sections mapped.
- **Type consistency:** `ensure_profile`, `run_scripted_login`, `invalidate`, `NeedsLoginError` names are consistent across T4/T6/T7; `render_html(..., profile=)` / `render(..., profile=)` / `run_login` / `create_live_session` / `complete_live_session` / `seed_and_save_profile` consistent across T3/T6/T7; `is_auth_wall(text, final_url, login_domain)` consistent across T5/T6/T7; `AuthRealmResponse` fields match the route `_response` builder.
- **Known verification point:** the BrowserQL `liveURL`/`reconnect` response shape (T3 Step 0) must be confirmed against the homelab Browserless before relying on `create_live_session`/`complete_live_session`; the task includes that spike and a documented fallback.
