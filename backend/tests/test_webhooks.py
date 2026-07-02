"""Tests for webhook configuration CRUD, signing, and dispatch.

Tests cover:
- CRUD operations (create, list, get, update, delete)
- Event type validation
- HMAC-SHA256 payload signature correctness
- Dispatcher delivery with retry (mocked HTTP)
- Delivery logging and stats updates
- Test ping endpoint
- Delivery history listing
- Fire-and-forget mode (db=None) does not raise
"""

import hashlib
import hmac
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models.webhook import WebhookConfig, WebhookDelivery
from app.models.vendor import Vendor
from app.models.product import Product
from app.models.source import DocumentationSource
from app.services.webhook_dispatcher import (
    _sign_payload,
    _build_payload,
    _events_list,
    dispatch_event,
    send_test_ping,
    SIGNATURE_HEADER,
    SIGNATURE_PREFIX,
)

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/docextractor_test"
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client():
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


# ── Helpers ──

async def _make_source(db) -> tuple[str, str, str]:
    """Create a vendor/product/source tuple and return their IDs as strings."""
    vendor = Vendor(name="TestVendor")
    db.add(vendor)
    await db.flush()
    product = Product(vendor_id=vendor.id, name="TestProduct")
    db.add(product)
    await db.flush()
    source = DocumentationSource(
        product_id=product.id, name="TestSource", base_url="https://docs.example.com",
    )
    db.add(source)
    await db.flush()
    return str(vendor.id), str(product.id), str(source.id)


class _FakeResponse:
    """A minimal fake httpx.Response for mocking."""
    def __init__(self, status_code=200, text="OK", reason_phrase="OK"):
        self.status_code = status_code
        self.text = text
        self.reason_phrase = reason_phrase


def _mock_response(status_code=200, text="OK"):
    """Create a fake httpx.Response with the correct interface."""
    return _FakeResponse(status_code=status_code, text=text)


def _patch_httpx_post(monkeypatch, handler):
    """Patch httpx.AsyncClient.post so calls to external URLs are handled by *handler*.

    Calls to the test client (base_url=http://test) are passed through to the
    real implementation so the ASGI transport still works.
    """
    import httpx
    original_post = httpx.AsyncClient.post

    async def patched_post(self, url, **kwargs):
        # Only intercept calls to real external URLs — not the test ASGI client
        # (which uses relative paths or http://test base URL).
        if isinstance(url, str) and (url.startswith("http://test") or url.startswith("/api/")):
            return await original_post(self, url, **kwargs)
        return await handler(self, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", patched_post)
    return original_post


def _make_test_ping_with_factory(test_factory):
    """Return a replacement for send_test_ping that uses the test DB factory."""
    from app.services.webhook_dispatcher import _build_payload, _deliver_one

    async def _test_ping(webhook):
        payload = _build_payload(
            event_type="test",
            run_id=None,
            source_id=webhook.source_id,
            source_name=None,
            vendor_name=None,
            product_name=None,
            extra={"message": "DocExtractor webhook test ping"},
        )
        return await _deliver_one(
            webhook=webhook,
            payload=payload,
            event_type="test",
            run_id=None,
            source_id=webhook.source_id,
            session_factory=test_factory,
        )

    return _test_ping



# ── Unit tests (no HTTP) ──

def test_sign_payload_hmac_sha256():
    """Signature is HMAC-SHA256 with sha256= prefix."""
    secret = "mysecret"
    body = b'{"event":"test"}'
    sig = _sign_payload(body, secret)
    assert sig.startswith(SIGNATURE_PREFIX)
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert sig == f"{SIGNATURE_PREFIX}{expected}"


def test_sign_payload_different_secrets_differ():
    body = b'{"event":"test"}'
    sig1 = _sign_payload(body, "secret1")
    sig2 = _sign_payload(body, "secret2")
    assert sig1 != sig2


def test_build_payload_structure():
    payload = _build_payload(
        event_type="extraction_complete",
        run_id=None,
        source_id=None,
        source_name="MySource",
        vendor_name="MyVendor",
        product_name="MyProduct",
        extra={"articles_extracted": 5},
    )
    assert payload["event"] == "extraction_complete"
    assert payload["source_name"] == "MySource"
    assert payload["vendor_name"] == "MyVendor"
    assert payload["product_name"] == "MyProduct"
    assert payload["articles_extracted"] == 5
    assert "timestamp" in payload


def test_events_list_parsing():
    wh = WebhookConfig(url="https://example.com/hook", events="new_page,updated_page")
    assert _events_list(wh) == {"new_page", "updated_page"}


def test_events_list_single():
    wh = WebhookConfig(url="https://example.com/hook", events="extraction_complete")
    assert _events_list(wh) == {"extraction_complete"}


# ── CRUD tests ──

async def test_create_webhook(client):
    resp = await client.post("/api/webhooks", json={
        "url": "https://example.com/hook",
        "label": "Slack CI",
        "events": ["extraction_complete", "new_page"],
        "secret": "mysecret",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["url"] == "https://example.com/hook"
    assert data["label"] == "Slack CI"
    assert set(data["events"]) == {"extraction_complete", "new_page"}
    assert data["secret"] == "mysecret"
    assert data["is_active"] is True
    assert data["total_deliveries"] == 0


async def test_create_webhook_invalid_event(client):
    resp = await client.post("/api/webhooks", json={
        "url": "https://example.com/hook",
        "events": ["bogus_event"],
    })
    assert resp.status_code == 422


async def test_list_webhooks(client):
    await client.post("/api/webhooks", json={"url": "https://a.com/hook", "label": "A"})
    await client.post("/api/webhooks", json={"url": "https://b.com/hook", "label": "B"})
    resp = await client.get("/api/webhooks")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["webhooks"]) == 2


async def test_get_webhook(client):
    create = await client.post("/api/webhooks", json={"url": "https://example.com/hook"})
    wid = create.json()["id"]
    resp = await client.get(f"/api/webhooks/{wid}")
    assert resp.status_code == 200
    assert resp.json()["id"] == wid


async def test_get_webhook_not_found(client):
    resp = await client.get("/api/webhooks/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


async def test_update_webhook(client):
    create = await client.post("/api/webhooks", json={
        "url": "https://example.com/hook",
        "events": ["extraction_complete"],
    })
    wid = create.json()["id"]
    resp = await client.patch(f"/api/webhooks/{wid}", json={
        "label": "Updated Label",
        "is_active": False,
        "events": ["new_page", "updated_page", "removed_page", "extraction_complete"],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["label"] == "Updated Label"
    assert data["is_active"] is False
    assert len(data["events"]) == 4


async def test_delete_webhook(client):
    create = await client.post("/api/webhooks", json={"url": "https://example.com/hook"})
    wid = create.json()["id"]
    resp = await client.delete(f"/api/webhooks/{wid}")
    assert resp.status_code == 204
    resp = await client.get(f"/api/webhooks/{wid}")
    assert resp.status_code == 404


async def test_list_webhooks_filter_active(client):
    await client.post("/api/webhooks", json={"url": "https://a.com/hook", "is_active": True})
    await client.post("/api/webhooks", json={"url": "https://b.com/hook", "is_active": False})
    resp = await client.get("/api/webhooks", params={"is_active": True})
    assert resp.json()["total"] == 1


# ── Delivery and dispatch tests ──

async def test_test_ping_endpoint(client, monkeypatch):
    """Test ping endpoint returns success when the HTTP POST succeeds."""
    create = await client.post("/api/webhooks", json={
        "url": "https://example.com/hook",
        "secret": "test_secret",
    })
    wid = create.json()["id"]

    async def mock_post(self, url, **kwargs):
        return _mock_response(200, "OK")

    _patch_httpx_post(monkeypatch, mock_post)

    # Patch the session factory used by send_test_ping to use the test DB.
    test_engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    test_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    import app.routes.webhooks as wr
    monkeypatch.setattr(wr, "send_test_ping", _make_test_ping_with_factory(test_factory))

    try:
        resp = await client.post(f"/api/webhooks/{wid}/test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["status_code"] == 200
    finally:
        await test_engine.dispose()


async def test_test_ping_failure(client, monkeypatch):
    """Test ping returns failure when the endpoint is unreachable."""
    create = await client.post("/api/webhooks", json={"url": "https://example.com/hook"})
    wid = create.json()["id"]

    async def mock_post(self, url, **kwargs):
        raise httpx.ConnectError("connection refused")

    _patch_httpx_post(monkeypatch, mock_post)

    test_engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    test_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    import app.routes.webhooks as wr
    monkeypatch.setattr(wr, "send_test_ping", _make_test_ping_with_factory(test_factory))

    try:
        resp = await client.post(f"/api/webhooks/{wid}/test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"] is not None
    finally:
        await test_engine.dispose()


async def test_deliveries_history(client, monkeypatch):
    """Delivery history is recorded and listable."""
    async def mock_post(self, url, **kwargs):
        return _mock_response(200, "OK")

    _patch_httpx_post(monkeypatch, mock_post)

    test_engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    test_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    import app.routes.webhooks as wr
    monkeypatch.setattr(wr, "send_test_ping", _make_test_ping_with_factory(test_factory))

    try:
        create = await client.post("/api/webhooks", json={"url": "https://example.com/hook"})
        wid = create.json()["id"]

        # Send a test ping to create a delivery record.
        await client.post(f"/api/webhooks/{wid}/test")

        resp = await client.get(f"/api/webhooks/{wid}/deliveries")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert data["deliveries"][0]["event_type"] == "test"
        assert data["deliveries"][0]["success"] is True
    finally:
        await test_engine.dispose()


async def test_dispatch_event_no_webhooks(client):
    """dispatch_event returns 0 when no webhooks are configured."""
    from app.core.database import async_session
    async with async_session() as db:
        count = await dispatch_event(db, "extraction_complete", source_id=None)
    assert count == 0


async def test_dispatch_event_fire_and_forget_no_db(client, monkeypatch):
    """dispatch_event with db=None does not raise and queries its own session."""
    async def mock_post(self, url, **kwargs):
        return _mock_response(200, "OK")

    _patch_httpx_post(monkeypatch, mock_post)

    # Create a webhook via the API (uses the fixture's engine).
    await client.post("/api/webhooks", json={
        "url": "https://example.com/hook",
        "events": ["extraction_complete"],
    })

    # dispatch_event with db=None uses the default async_session factory.
    # In tests, this points to the real DB — so pass a session_factory that
    # uses the test database. We patch async_session to use the test URL.
    test_engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    test_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    try:
        count = await dispatch_event(None, "extraction_complete", session_factory=test_factory)
        assert count == 1
    finally:
        await test_engine.dispose()


async def test_hmac_signature_sent_in_header(client, monkeypatch):
    """The X-DocExtractor-Signature header is sent when a secret is configured."""
    captured_headers = {}

    async def mock_post(self, url, **kwargs):
        captured_headers.update(kwargs.get("headers", {}))
        return _mock_response(200, "OK")

    _patch_httpx_post(monkeypatch, mock_post)

    # Create a webhook with a secret via the API.
    await client.post("/api/webhooks", json={
        "url": "https://example.com/hook",
        "events": ["extraction_complete"],
        "secret": "my_hmac_secret",
    })

    test_engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    test_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    try:
        count = await dispatch_event(None, "extraction_complete", session_factory=test_factory)
        assert count == 1
        assert SIGNATURE_HEADER in captured_headers
        sig = captured_headers[SIGNATURE_HEADER]
        assert sig.startswith(SIGNATURE_PREFIX)
    finally:
        await test_engine.dispose()


async def test_no_signature_when_secret_is_none(client, monkeypatch):
    """No X-DocExtractor-Signature header when secret is None."""
    captured_headers = {}

    async def mock_post(self, url, **kwargs):
        captured_headers.update(kwargs.get("headers", {}))
        return _mock_response(200, "OK")

    _patch_httpx_post(monkeypatch, mock_post)

    # Create a webhook without a secret.
    await client.post("/api/webhooks", json={
        "url": "https://example.com/hook",
        "events": ["extraction_complete"],
    })

    test_engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    test_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    try:
        count = await dispatch_event(None, "extraction_complete", session_factory=test_factory)
        assert count == 1
        assert SIGNATURE_HEADER not in captured_headers
    finally:
        await test_engine.dispose()


async def test_retry_on_5xx(client, monkeypatch):
    """Dispatcher retries on 5xx responses."""
    call_count = 0

    async def mock_post(self, url, **kwargs):
        nonlocal call_count
        call_count += 1
        return _mock_response(503, "Service Unavailable")

    # Speed up retries by patching RETRY_DELAYS.
    from app.services import webhook_dispatcher as wd
    original_delays = wd.RETRY_DELAYS
    wd.RETRY_DELAYS = [0, 0, 0]

    _patch_httpx_post(monkeypatch, mock_post)

    # Create a webhook via the API.
    await client.post("/api/webhooks", json={
        "url": "https://example.com/hook",
        "events": ["extraction_complete"],
    })

    test_engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    test_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    try:
        count = await dispatch_event(None, "extraction_complete", session_factory=test_factory)
        # 3 attempts but delivery is not successful.
        assert call_count == 3
        assert count == 1  # one delivery attempted (even though it failed)
    finally:
        wd.RETRY_DELAYS = original_delays
        await test_engine.dispose()


async def test_source_scoped_webhook(client, monkeypatch):
    """A source-scoped webhook only fires for that source."""
    received_urls = []

    async def mock_post(self, url, **kwargs):
        received_urls.append(url)
        return _mock_response(200, "OK")

    _patch_httpx_post(monkeypatch, mock_post)

    # Create a source via the API.
    vendor_resp = await client.post("/api/vendors", json={"name": "TestVendor"})
    vendor_id = vendor_resp.json()["id"]
    product_resp = await client.post("/api/products", json={"vendor_id": vendor_id, "name": "TestProduct"})
    product_id = product_resp.json()["id"]
    source_resp = await client.post("/api/sources", json={
        "product_id": product_id,
        "name": "TestSource",
        "base_url": "https://docs.example.com",
    })
    source_id = source_resp.json()["id"]

    # Create a webhook scoped to this source.
    await client.post("/api/webhooks", json={
        "url": "https://scoped.example.com/hook",
        "events": ["extraction_complete"],
        "source_id": source_id,
    })
    # Create a global webhook.
    await client.post("/api/webhooks", json={
        "url": "https://global.example.com/hook",
        "events": ["extraction_complete"],
    })

    test_engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    test_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    try:
        # Dispatch for global only (source_id=None) → only the global webhook fires.
        count = await dispatch_event(None, "extraction_complete", source_id=None, session_factory=test_factory)
        assert count == 1
        assert "https://global.example.com/hook" in received_urls

        received_urls.clear()
        # Dispatch for the specific source → both global and scoped fire.
        count = await dispatch_event(None, "extraction_complete", source_id=source_id, session_factory=test_factory)
        assert count == 2
        assert "https://scoped.example.com/hook" in received_urls
        assert "https://global.example.com/hook" in received_urls
    finally:
        await test_engine.dispose()