"""Periodically pushes generator output into a point store."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Mapping

from fieldsim.core.datastore import PointStore
from fieldsim.core.generators import Generator

logger = logging.getLogger(__name__)


class GeneratorEngine:
    """Runs every generator of a device on a fixed interval.

    A point can be *pinned*: its generator is paused so a value set by a test
    stays put instead of being overwritten on the next tick.
    """

    def __init__(
        self,
        store: PointStore,
        generators: Mapping[str, Generator],
        *,
        interval: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        if interval <= 0:
            raise ValueError("interval must be > 0")
        self._store = store
        self._generators = dict(generators)
        self._interval = interval
        self._clock = clock
        self._pinned: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._t0 = 0.0

    @property
    def generated(self) -> frozenset[str]:
        return frozenset(self._generators)

    @property
    def pinned(self) -> frozenset[str]:
        return frozenset(self._pinned)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def pin(self, name: str) -> None:
        if name in self._generators:
            self._pinned.add(name)

    def release(self, name: str) -> None:
        self._pinned.discard(name)

    def tick(self, elapsed: float) -> None:
        """Sample every unpinned generator at ``elapsed`` seconds and store the result."""
        for name, generator in self._generators.items():
            if name not in self._pinned:
                self._store.set(name, generator.sample(elapsed), clamp=True)

    def start(self) -> None:
        if self.running or not self._generators:
            return
        self._t0 = self._clock()
        self.tick(0.0)
        self._task = asyncio.get_running_loop().create_task(self._run(), name="fieldsim-engine")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                self.tick(self._clock() - self._t0)
            except Exception:  # keep the device alive; one bad generator must not stop the rest
                logger.exception("generator tick failed")
