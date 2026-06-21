"""Unit tests for TocBuildCheckpoint — read/modify/write of the JSONB row,
using an in-memory fake session factory (no Postgres needed)."""
import sys
import os
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.toc_checkpoint import TocBuildCheckpoint
from app.models.toc_checkpoint import TocCheckpoint

SID = uuid.uuid4()


class FakeSession:
    """Minimal async session over a shared {source_id: TocCheckpoint} store."""

    def __init__(self, store):
        self._store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, model, pk):
        return self._store.get(pk)

    def add(self, obj):
        self._store[obj.source_id] = obj

    async def execute(self, stmt):
        # Only used for clear(): delete(TocCheckpoint).where(source_id == SID)
        self._store.pop(SID, None)

    async def commit(self):
        pass


def make_factory(store):
    def factory():
        return FakeSession(store)
    return factory


@pytest.mark.asyncio
async def test_load_empty_returns_empty_dict():
    cp = TocBuildCheckpoint(make_factory({}), SID)
    assert await cp.load() == {}


@pytest.mark.asyncio
async def test_save_top_level_then_section_accumulates():
    store = {}
    cp = TocBuildCheckpoint(make_factory(store), SID)
    await cp.save_top_level([{"id": "a"}, {"id": "b"}])
    await cp.save_section("a", [{"id": "a", "level": 0}])
    await cp.save_section("b", [{"id": "b", "level": 0}])

    data = await cp.load()
    assert data["top_level"] == [{"id": "a"}, {"id": "b"}]
    assert set(data["sections"]) == {"a", "b"}
    assert data["sections"]["a"] == [{"id": "a", "level": 0}]


@pytest.mark.asyncio
async def test_save_section_reassigns_data_not_mutates():
    """The stored row's .data must be reassigned (a new dict) so SQLAlchemy
    flushes the JSONB change rather than silently dropping an in-place mutation."""
    store = {}
    cp = TocBuildCheckpoint(make_factory(store), SID)
    await cp.save_section("a", [1])
    first = store[SID].data
    await cp.save_section("b", [2])
    second = store[SID].data
    assert first is not second  # new dict each write
    assert set(second["sections"]) == {"a", "b"}


@pytest.mark.asyncio
async def test_clear_removes_row():
    store = {SID: TocCheckpoint(source_id=SID, data={"sections": {"a": []}})}
    cp = TocBuildCheckpoint(make_factory(store), SID)
    await cp.clear()
    assert SID not in store
    assert await cp.load() == {}
