"""Work limits use fake clocks and child tasks; no network or bot is started."""

import asyncio

import pytest

from droppoint.limits import (
    Admission,
    TripWork,
    WindowLimit,
    WorkLimitExceeded,
    gather_cancel,
    trip_scope,
)


async def test_nested_failure_cancels_siblings_before_scope_cleanup():
    work = TripWork()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def late_writer():
        started.set()
        try:
            await asyncio.Event().wait()
            work.cache["synthetic-coordinates"] = 12
        finally:
            cancelled.set()

    async def fails():
        await started.wait()
        raise ValueError("mock route failed")

    with pytest.raises(ValueError, match="mock route failed"):
        async with trip_scope(work):
            await gather_cancel(gather_cancel(late_writer()), fails())
    assert cancelled.is_set()
    await asyncio.sleep(0)
    assert work.cache == {}


async def test_work_timeout_awaits_child_cancellation_and_clears_cache():
    work = TripWork(remaining_seconds=0.01)
    stopped = asyncio.Event()

    async def waits():
        try:
            work.cache["synthetic"] = 1
            await asyncio.Event().wait()
        finally:
            stopped.set()

    with pytest.raises(TimeoutError):
        async with trip_scope(work):
            await gather_cancel(waits())
    assert stopped.is_set()
    assert not work.cache


async def test_attempts_span_phases_but_cache_does_not():
    work = TripWork(max_attempts=2)
    async with trip_scope(work):
        work.claim()
        work.cache["first-phase"] = 1
    assert not work.cache
    async with trip_scope(work):
        work.claim()
        with pytest.raises(WorkLimitExceeded):
            work.claim()
    assert work.attempts == 2


def test_window_releases_capacity_and_never_records_rejections():
    now = [0.0]
    window = WindowLimit(2, clock=lambda: now[0])
    window.claim()
    window.claim()
    with pytest.raises(WorkLimitExceeded):
        window.claim()
    assert len(window.events) == 2
    now[0] = 60.0
    window.claim()
    assert len(window.events) == 1


def test_admission_cannot_be_reset_by_starting_a_new_conversation():
    now = [0.0]
    admission = Admission(maximum=2, cooldown=60, clock=lambda: now[0])
    admission.claim(100)
    with pytest.raises(WorkLimitExceeded):
        admission.claim(100)
    admission.claim(200)
    with pytest.raises(WorkLimitExceeded):
        admission.claim(300)
    now[0] = 60
    admission.claim(100)
    assert admission.users == {100: 60}
