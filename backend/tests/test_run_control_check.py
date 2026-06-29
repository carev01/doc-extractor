"""_raise_if_controlled is the cooperative cancel/pause check used at each
content chunk boundary on BOTH the raw_http and browserless paths, so a long
scrape honours a cancel/pause promptly instead of running to completion."""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.services.firecrawl import FirecrawlService, RunControlSignal


class _Result:
    def __init__(self, val):
        self._val = val

    def scalar_one_or_none(self):
        return self._val


class _DB:
    def __init__(self, val):
        self._val = val

    async def execute(self, stmt):
        return _Result(self._val)


@pytest.mark.asyncio
async def test_raises_cancel_signal():
    svc = FirecrawlService()
    with pytest.raises(RunControlSignal) as ei:
        await svc._raise_if_controlled(_DB("cancel"), uuid.uuid4())
    assert ei.value.action == "cancel"


@pytest.mark.asyncio
async def test_raises_pause_signal():
    svc = FirecrawlService()
    with pytest.raises(RunControlSignal) as ei:
        await svc._raise_if_controlled(_DB("pause"), uuid.uuid4())
    assert ei.value.action == "pause"


@pytest.mark.asyncio
async def test_no_signal_does_not_raise():
    svc = FirecrawlService()
    await svc._raise_if_controlled(_DB(None), uuid.uuid4())  # must not raise
