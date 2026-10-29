"""Finite work accounting for the supported single-process polling deployment."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, TypeVar

T = TypeVar("T")


class WorkLimitExceeded(Exception):
    """A request was stopped before spending more work or retaining more trip data."""


@dataclass
class TripWork:
    """One trip's attempt allowance and remaining active-work time (not thinking time)."""

    max_attempts: int = 300
    remaining_seconds: float = 180.0
    attempts: int = 0
    cache: dict[Any, Any] = field(default_factory=dict, repr=False)

    def claim(self) -> None:
        if self.attempts >= self.max_attempts:
            raise WorkLimitExceeded("This trip reached its request allowance.")
        self.attempts += 1


CURRENT_TRIP: ContextVar[TripWork | None] = ContextVar("droppoint_trip", default=None)


@asynccontextmanager
async def trip_scope(work: TripWork):
    """Keep counters across input phases, but never retain a phase's cached locations."""
    if work.remaining_seconds <= 0:
        raise WorkLimitExceeded("This trip reached its work deadline.")
    token = CURRENT_TRIP.set(work)
    started = time.monotonic()
    try:
        async with asyncio.timeout(work.remaining_seconds):
            yield
    finally:
        work.remaining_seconds = max(0.0, work.remaining_seconds - (time.monotonic() - started))
        work.cache.clear()
        CURRENT_TRIP.reset(token)


async def gather_cancel(*awaitables: Awaitable[T]) -> list[T]:
    """Cancel and await every child on failure, preserving the original exception type."""
    tasks = [asyncio.ensure_future(item) for item in awaitables]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


class WindowLimit:
    """A bounded sliding window; callers must share it within one polling process."""

    def __init__(self, maximum: int, seconds: float = 60.0, *, clock=time.monotonic):
        if maximum < 1 or seconds <= 0:
            raise ValueError("Work limits must be positive.")
        self.maximum = maximum
        self.seconds = seconds
        self.clock = clock
        self.events: deque[float] = deque()

    def claim(self) -> None:
        now = self.clock()
        while self.events and self.events[0] <= now - self.seconds:
            self.events.popleft()
        if len(self.events) >= self.maximum:
            raise WorkLimitExceeded("The bot is busy. Please try again later.")
        self.events.append(now)


class Admission:
    """Limit trip starts without keeping user identifiers beyond the cooldown window."""

    def __init__(self, maximum: int = 20, cooldown: float = 60.0, *, clock=time.monotonic):
        if cooldown <= 0:
            raise ValueError("The user cooldown must be positive.")
        self.starts = WindowLimit(maximum, clock=clock)
        self.cooldown = cooldown
        self.clock = clock
        self.users: dict[int, float] = {}

    def claim(self, user_id: int) -> None:
        now = self.clock()
        self.users = {user: at for user, at in self.users.items() if at > now - self.cooldown}
        if user_id in self.users:
            raise WorkLimitExceeded("Please wait before starting another trip.")
        self.starts.claim()
        self.users[user_id] = now
