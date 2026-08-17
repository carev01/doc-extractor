"""Cancelling a run during TOC discovery.

The chunk-boundary ``_raise_if_controlled`` checks only cover the content phase.
TOC discovery is a single long await (a sidebar expansion can run for minutes, and
a Browserless session cap is 30min), so a cancel issued during discovery used to be
invisible until that call returned — the run looked unkillable and the API's
cooperative cancel silently did nothing. ``_await_watching_control`` races the work
against a control poller so discovery is cancellable too.

The poller is exercised with a fake ``async_session`` so these stay DB-free.
"""

import asyncio
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import firecrawl as fc_mod
from app.services.firecrawl import FirecrawlService, RunControlSignal

pytestmark = pytest.mark.asyncio

RUN_ID = uuid.uuid4()


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    """Async-context-manager session whose control query returns a scripted value.

    The script is shared across sessions (the watcher opens a fresh session per
    poll), so a sequence like [None, None, "cancel"] advances poll by poll.
    """

    def __init__(self, values):
        self._values = values

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *_a, **_kw):
        # Consume until the last value, then keep returning it.
        return _FakeResult(self._values.pop(0) if len(self._values) > 1 else self._values[0])


def _patch_control(monkeypatch, values):
    shared = list(values)  # one script, shared by every per-poll session
    monkeypatch.setattr(fc_mod, "async_session", lambda: _FakeSession(shared))


async def test_returns_result_when_no_control_signal(monkeypatch):
    _patch_control(monkeypatch, [None])
    svc = FirecrawlService()
    svc.CONTROL_POLL_INTERVAL = 0.01

    async def work():
        await asyncio.sleep(0.05)
        return ["entry-a", "entry-b"]

    assert await svc._await_watching_control(RUN_ID, work()) == ["entry-a", "entry-b"]


async def test_cancel_during_discovery_raises_and_aborts_the_build(monkeypatch):
    # The signal must surface promptly *and* the in-flight build must be aborted,
    # not left running in the background holding the Browserless session.
    _patch_control(monkeypatch, ["cancel"])
    svc = FirecrawlService()
    svc.CONTROL_POLL_INTERVAL = 0.01
    finished = False

    async def slow_build():
        nonlocal finished
        await asyncio.sleep(5)      # stands in for a 30-min expansion
        finished = True
        return ["never"]

    with pytest.raises(RunControlSignal) as excinfo:
        await svc._await_watching_control(RUN_ID, slow_build())
    assert excinfo.value.action == "cancel"
    await asyncio.sleep(0.02)       # give a stray task a chance to run
    assert finished is False        # the build was cancelled, not orphaned


async def test_pause_during_discovery_surfaces_pause_action(monkeypatch):
    _patch_control(monkeypatch, ["pause"])
    svc = FirecrawlService()
    svc.CONTROL_POLL_INTERVAL = 0.01

    async def slow_build():
        await asyncio.sleep(5)
        return []

    with pytest.raises(RunControlSignal) as excinfo:
        await svc._await_watching_control(RUN_ID, slow_build())
    assert excinfo.value.action == "pause"


async def test_signal_arriving_mid_flight_is_picked_up(monkeypatch):
    # First poll sees nothing, a later poll sees the cancel — the run must not have
    # to wait for the build to return.
    _patch_control(monkeypatch, [None, None, "cancel"])
    svc = FirecrawlService()
    svc.CONTROL_POLL_INTERVAL = 0.01

    async def slow_build():
        await asyncio.sleep(5)
        return []

    with pytest.raises(RunControlSignal):
        await svc._await_watching_control(RUN_ID, slow_build())


async def test_build_error_propagates_unchanged(monkeypatch):
    # A genuine failure inside the build must not be masked by the watcher.
    _patch_control(monkeypatch, [None])
    svc = FirecrawlService()
    svc.CONTROL_POLL_INTERVAL = 0.01

    async def failing_build():
        await asyncio.sleep(0.01)
        raise ValueError("browserless exploded")

    with pytest.raises(ValueError, match="browserless exploded"):
        await svc._await_watching_control(RUN_ID, failing_build())
