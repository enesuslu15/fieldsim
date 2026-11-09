"""Base class shared by every simulated device."""

from __future__ import annotations

import asyncio
import contextlib
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any, Generic, Protocol, TypeVar

from fieldsim.config import FaultSpec, ScenarioStep
from fieldsim.core.clock import Clock, RealClock
from fieldsim.core.datastore import PointStore, Value
from fieldsim.core.engine import GeneratorEngine
from fieldsim.core.faults import ActiveFault, FaultInjector, parse_fault
from fieldsim.core.generators import Generator
from fieldsim.core.scenario import ScenarioRunner

StoreT = TypeVar("StoreT", bound=PointStore)


class WriteRecord(Protocol):
    """A write performed by a client. Each protocol adds its own details."""

    @property
    def points(self) -> tuple[str, ...]: ...


class Device(ABC, Generic[StoreT]):
    """A running (or startable) virtual device.

    Subclasses only implement the network side (:meth:`_start_server`,
    :meth:`_stop_server`, :attr:`host`, :attr:`port`). Values, generators,
    faults, scenarios and outages are shared by every protocol.

    All methods must be called from the event loop that runs the device. For
    use from other threads (for example synchronous tests) see
    :class:`fieldsim.BackgroundSimulator`.
    """

    protocol: str = ""

    def __init__(
        self,
        name: str,
        store: StoreT,
        generators: Mapping[str, Generator],
        *,
        update_interval: float,
        faults: Iterable[FaultSpec] = (),
        scenario: Sequence[ScenarioStep] = (),
        seed: int | None = None,
        clock: Clock | None = None,
    ):
        self.name = name
        self.store = store
        self.clock: Clock = clock or RealClock()
        self.faults = FaultInjector(seed=seed)
        for spec in faults:
            self.add_fault(spec)
        self._engine = GeneratorEngine(
            store, generators, interval=update_interval, clock=self.clock
        )
        self.scenario = ScenarioRunner(self, scenario, self.clock)
        self._running = False
        self._online = False
        #: Every client write, in order. Protocols append via :meth:`_record_write`.
        self.writes: list[Any] = []
        self._write_cursor = 0
        self._write_signal = asyncio.Event()

    # ------------------------------------------------------------------ network

    @property
    @abstractmethod
    def host(self) -> str: ...

    @property
    @abstractmethod
    def port(self) -> int:
        """Listening port. After :meth:`start` this is the real port, even if 0 was configured."""

    @property
    def address(self) -> tuple[str, int]:
        return self.host, self.port

    @abstractmethod
    async def _start_server(self) -> None: ...

    @abstractmethod
    async def _stop_server(self) -> None:
        """Stop accepting clients. Also used for outages, so it must be restartable."""

    async def _shutdown(self) -> None:
        """Release everything else when the device stops for good. Optional."""

    # ------------------------------------------------------------------ lifecycle

    @property
    def running(self) -> bool:
        return self._running

    @property
    def online(self) -> bool:
        """True while the device accepts connections (False during an outage)."""
        return self._online

    async def start(self) -> None:
        if self._running:
            return
        try:
            await self._start_server()
        except BaseException:
            await self._shutdown()
            raise
        self._online = True
        self._running = True
        self._engine.start()
        self.scenario.start()

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        await self.scenario.stop()
        await self._engine.stop()
        if self._online:
            self._online = False
            await self._stop_server()
        await self._shutdown()

    async def go_offline(self) -> None:
        """Simulate a power or network loss: drop every client and refuse new ones.

        Values and generators keep running, as they would on a device whose
        network link went down.
        """
        self._require_running()
        if self._online:
            self._online = False
            await self._stop_server()

    async def go_online(self) -> None:
        """End an outage; the device listens on the same port again."""
        self._require_running()
        if not self._online:
            await self._start_server()
            self._online = True

    @asynccontextmanager
    async def offline(self) -> AsyncIterator[None]:
        """``async with device.offline(): ...`` - outage for the duration of the block."""
        await self.go_offline()
        try:
            yield
        finally:
            if self._running:
                await self.go_online()

    def _require_running(self) -> None:
        if not self._running:
            raise RuntimeError(f"device {self.name!r} is not running")

    async def __aenter__(self) -> Device[StoreT]:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()

    # ------------------------------------------------------------------ faults

    def validate_fault(self, spec: FaultSpec) -> None:
        """Reject faults that make no sense for this protocol. Overridden by subclasses."""

    def add_fault(self, spec: FaultSpec) -> ActiveFault:
        self.validate_fault(spec)
        return self.faults.add(spec)

    def fault(self, fault_type: str, **params: Any) -> ActiveFault:
        """Activate a fault, e.g. ``device.fault("latency", delay="200ms")``.

        The returned object is a context manager that removes the fault again::

            with device.fault("drop", count=2) as f:
                ...
            assert f.hits == 2
        """
        return self.add_fault(parse_fault(fault_type, **params))

    def clear_faults(self) -> None:
        self.faults.clear()

    # ------------------------------------------------------------------ write log

    def _record_write(self, event: WriteRecord) -> None:
        self.writes.append(event)
        self._write_signal.set()

    def clear_writes(self) -> None:
        self.writes.clear()
        self._write_cursor = 0

    async def wait_for_write(self, point: str | None = None, timeout: float = 5.0) -> Any:
        """Wait for a client write (touching ``point``, if given) and return it.

        Writes are consumed in order: each call returns the first matching write
        after the one the previous call returned, including writes that happened
        before the call. Raises :class:`TimeoutError` if none arrives in time.
        """
        if point is not None:
            self.store.get(point)  # validates the name
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for index in range(self._write_cursor, len(self.writes)):
                event = self.writes[index]
                if point is None or point in event.points:
                    self._write_cursor = index + 1
                    return event
            remaining = deadline - loop.time()
            if remaining <= 0:
                target = f"to {point!r} " if point else ""
                raise TimeoutError(f"no write {target}within {timeout}s on {self.name!r}")
            self._write_signal.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._write_signal.wait(), remaining)

    # ------------------------------------------------------------------ values

    def get(self, name: str) -> Value:
        """Current value of a point."""
        return self.store.get(name)

    def set(self, name: str, value: Value) -> None:
        """Set a point's value.

        If the point is driven by a generator, the generator is paused until
        :meth:`release` is called, so the value stays exactly as set.
        """
        self.store.set(name, value)
        self._engine.pin(name)

    def release(self, name: str) -> None:
        """Hand a point set with :meth:`set` back to its generator."""
        self.store.get(name)  # validates the name
        self._engine.release(name)

    @property
    def generated_points(self) -> frozenset[str]:
        return self._engine.generated

    def __repr__(self) -> str:
        state = "stopped" if not self._running else "running" if self._online else "offline"
        return f"<{type(self).__name__} {self.name!r} {self.host}:{self.port} {state}>"
