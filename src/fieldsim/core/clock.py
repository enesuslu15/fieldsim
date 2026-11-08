"""Clocks that drive generators and scenarios.

:class:`RealClock` follows wall time. :class:`VirtualClock` only moves when
:meth:`VirtualClock.advance` is called, so a test can play a 10 minute scenario
in milliseconds and still get exactly the same sequence of events every run.

Network behaviour (latency faults, socket timeouts) always uses real time.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from typing import Protocol


class Clock(Protocol):
    def time(self) -> float:
        """Current time in seconds (arbitrary origin)."""
        ...

    async def sleep(self, delay: float) -> None:
        """Suspend the caller for ``delay`` seconds of this clock's time."""
        ...


class RealClock:
    def time(self) -> float:
        return time.monotonic()

    async def sleep(self, delay: float) -> None:
        await asyncio.sleep(max(delay, 0.0))


class VirtualClock:
    """A clock that stands still until told to move.

    All coroutines sleeping on it are woken in deadline order while
    :meth:`advance` runs, and each gets the chance to schedule its next sleep
    before time moves on. Use it from a single event loop.
    """

    #: Event-loop iterations granted to woken tasks before time moves on.
    settle_iterations = 8

    def __init__(self, start: float = 0.0):
        self._now = start
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._counter = itertools.count()

    def time(self) -> float:
        return self._now

    @property
    def pending(self) -> int:
        """Number of coroutines currently sleeping on this clock."""
        return sum(1 for *_, fut in self._sleepers if not fut.done())

    async def sleep(self, delay: float) -> None:
        if delay <= 0:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self._now + delay, next(self._counter), future))
        await future

    async def advance(self, seconds: float) -> None:
        """Move time forward by ``seconds``, running everything that falls due."""
        if seconds < 0:
            raise ValueError("cannot move a clock backwards")
        target = self._now + seconds
        await self._settle()
        while self._sleepers and self._sleepers[0][0] <= target:
            deadline, _, future = heapq.heappop(self._sleepers)
            if future.done():  # sleeper was cancelled
                continue
            self._now = max(self._now, deadline)
            future.set_result(None)
            await self._settle()
        self._now = target
        await self._settle()

    async def _settle(self) -> None:
        for _ in range(self.settle_iterations):
            await asyncio.sleep(0)
