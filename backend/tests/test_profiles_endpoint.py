"""GET /api/profiles — the platform options the UI renders, sourced from the
backend profile registry so the dropdown can't drift out of sync.

DB-free: the endpoint only reads the in-memory registry, so these run without a
database (plain ASGITransport, no lifespan).
"""

import os
import sys

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.main import app
from app.services.profiles import registry

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_auto_detect_is_first_option(client):
    data = (await client.get("/api/profiles")).json()
    assert data[0] == {"value": "auto", "label": "Auto-detect"}


async def test_mirrors_registry_in_order_after_auto(client):
    data = (await client.get("/api/profiles")).json()
    values = [o["value"] for o in data]
    assert values == ["auto"] + [p.name for p in registry.PROFILES]


async def test_includes_current_profiles_and_no_stale_dell(client):
    values = [o["value"] for o in (await client.get("/api/profiles")).json()]
    assert "warmup_listgroup" in values
    assert "category_accordion" in values
    assert "release_notes" in values
    assert "dell" not in values


async def test_uses_curated_labels(client):
    labels = {o["value"]: o["label"] for o in (await client.get("/api/profiles")).json()}
    assert labels["warmup_listgroup"] == "Warm-up + List Group"
    assert labels["mkdocs"] == "MkDocs"
    assert labels["category_accordion"] == "Category Accordion"
