"""Plays a device's scenario: a timeline of value changes, faults and outages."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from fieldsim.config import FaultSpec, ScenarioStep
from fieldsim.core.clock import Clock
from fieldsim.core.datastore import Value
from fieldsim.core.faults import ActiveFault

logger = logging.getLogger(__name__)


class ScenarioTarget(Protocol):
    def set(self, name: str, value: Value) -> None: ...

    def release(self, name: str) -> None: ...

    def add_fault(self, spec: FaultSpec) -> ActiveFault: ...

    async def go_offline(self) -> None: ...

    async def go_online(self) -> None: ...


Action = Callable[[], Awaitable[None] | None]


@dataclass(frozen=True)
class ScenarioEvent:
    time: float
    description: str
    action: Action


def build_events(target: ScenarioTarget, steps: Sequence[ScenarioStep]) -> list[ScenarioEvent]:
    """Expand steps into a sorted list of instantaneous events.

    A step with a duration becomes two events (start and end). Events at the
    same time keep the order in which they were written.
    """
    events: list[tuple[float, int, ScenarioEvent]] = []

    def add(time: float, description: str, action: Action) -> None:
        events.append((time, len(events), ScenarioEvent(time, description, action)))

    for step in steps:
        if step.set is not None:
            values = dict(step.set)

            def do_set(values: dict[str, Value] = values) -> None:
                for name, value in values.items():
                    target.set(name, value)

            add(step.at, f"set {values}", do_set)
        elif step.release is not None:
            names = list(step.release)

            def do_release(names: list[str] = names) -> None:
                for name in names:
                    target.release(name)

            add(step.at, f"release {', '.join(names)}", do_release)
        elif step.fault is not None:
            holder: list[ActiveFault] = []
            spec = step.fault

            def do_fault(spec: FaultSpec = spec, holder: list[ActiveFault] = holder) -> None:
                holder.append(target.add_fault(spec))

            add(step.at, f"fault {spec.type} on", do_fault)
            if step.duration is not None:

                def undo_fault(holder: list[ActiveFault] = holder) -> None:
                    for fault in holder:
                        fault.remove()

                add(step.at + step.duration, f"fault {spec.type} off", undo_fault)
        elif step.offline is not None:
            add(step.at, "offline", target.go_offline)
            add(step.at + step.offline, "online", target.go_online)

    return [event for *_, event in sorted(events, key=lambda e: (e[0], e[1]))]


class ScenarioRunner:
    def __init__(self, target: ScenarioTarget, steps: Sequence[ScenarioStep], clock: Clock):
        self.events = build_events(target, steps)
        self.history: list[ScenarioEvent] = []
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._t0 = 0.0

    @property
    def done(self) -> bool:
        return len(self.history) == len(self.events)

    @property
    def elapsed(self) -> float:
        return self._clock.time() - self._t0

    def start(self) -> None:
        if self._task is not None or not self.events:
            return
        self._t0 = self._clock.time()
        self._task = asyncio.get_running_loop().create_task(self._run(), name="fieldsim-scenario")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def wait(self) -> None:
        """Wait until every event has run (returns at once if there are none)."""
        if self._task is not None:
            await asyncio.shield(self._task)

    async def _run(self) -> None:
        for event in self.events:
            delay = self._t0 + event.time - self._clock.time()
            if delay > 0:
                await self._clock.sleep(delay)
            logger.info("scenario t=%.3fs: %s", event.time, event.description)
            try:
                result = event.action()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("scenario action failed: %s", event.description)
            self.history.append(event)
