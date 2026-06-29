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
from app.models.source import DocumentationSource
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
