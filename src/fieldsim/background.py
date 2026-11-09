"""Run a simulation in a background thread and control it from synchronous code.

The devices live on their own event loop in a daemon thread, so they work the
same whether the code under test is synchronous, asyncio, trio or a separate
process. Every method of :class:`DeviceHandle` is thread-safe.

::

    with BackgroundSimulator("plant.yaml") as sim:
        plc = sim["boiler-plc"]
        plc.set("temperature", 85.0)
        with plc.fault("latency", delay="300ms"):
            run_my_gateway(plc.host, plc.port)
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, TypeVar

from fieldsim.config import SimulationConfig, load_config, parse_config
from fieldsim.core.clock import Clock, RealClock, VirtualClock
from fieldsim.core.datastore import Value
from fieldsim.core.device import Device
from fieldsim.core.faults import ActiveFault
from fieldsim.simulator import Simulator

T = TypeVar("T")

ConfigSource = SimulationConfig | Mapping[str, Any] | str | Path
ClockKind = Literal["real", "virtual"]

CALL_TIMEOUT = 10.0
#: Generous: the first OPC UA device of a process builds the standard address
#: space, which takes tens of seconds under a coverage tracer.
STARTUP_TIMEOUT = 120.0


def resolve_config(source: ConfigSource) -> SimulationConfig:
    """Accept a config object, a dict or a path to a YAML file."""
    if isinstance(source, SimulationConfig):
        return source
    if isinstance(source, str | Path):
        return load_config(source)
    return parse_config(dict(source))


def with_free_ports(config: SimulationConfig) -> SimulationConfig:
    """Copy of ``config`` where every device listens on an OS-assigned port.

    Lets tests run in parallel (pytest-xdist) without port clashes.
    """
    devices = [device.model_copy(update={"port": 0}) for device in config.devices]
    return config.model_copy(update={"devices": devices})


class BackgroundSimulator:
    def __init__(
        self,
        config: ConfigSource,
        *,
        clock: ClockKind = "real",
        free_ports: bool = False,
    ):
        if clock not in ("real", "virtual"):
            raise ValueError(f"clock must be 'real' or 'virtual', not {clock!r}")
        resolved = resolve_config(config)
        self.config = with_free_ports(resolved) if free_ports else resolved
        self.clock_kind: ClockKind = clock
        self._clock: Clock = VirtualClock() if clock == "virtual" else RealClock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop_event: asyncio.Event | None = None
        self._error: BaseException | None = None
        self._handles: dict[str, DeviceHandle] = {}

    # ------------------------------------------------------------------ lifecycle

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, timeout: float = STARTUP_TIMEOUT) -> None:
        if self._thread is not None:
            raise RuntimeError("simulator already started")
        ready = threading.Event()
        self._error = None
        self._thread = threading.Thread(
            target=self._thread_main, args=(ready,), name="fieldsim", daemon=True
        )
        self._thread.start()
        if not ready.wait(timeout):  # pragma: no cover - only on a wedged machine
            raise TimeoutError("simulator did not start in time")
        if self._error is not None:
            self._thread.join()
            self._thread = None
            raise self._error

    def stop(self, timeout: float = CALL_TIMEOUT) -> None:
        thread, self._thread = self._thread, None
        if thread is None:
            return
        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        thread.join(timeout)
        self._handles = {}

    def __enter__(self) -> BackgroundSimulator:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    def _thread_main(self, ready: threading.Event) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self._serve(ready))
        finally:
            loop.close()

    async def _serve(self, ready: threading.Event) -> None:
        self._stop_event = asyncio.Event()
        try:
            sim = Simulator(self.config, clock=self._clock)
            await sim.start()
        except BaseException as exc:
            self._error = exc
            ready.set()
            return
        self._handles = {name: DeviceHandle(self, dev) for name, dev in sim.devices.items()}
        ready.set()
        try:
            await self._stop_event.wait()
        finally:
            await sim.stop()

    # ------------------------------------------------------------------ access

    @property
    def devices(self) -> dict[str, DeviceHandle]:
        return dict(self._handles)

    def __getitem__(self, name: str) -> DeviceHandle:
        try:
            return self._handles[name]
        except KeyError:
            available = ", ".join(self._handles) or "none (simulator not running)"
            raise KeyError(f"no device named {name!r}; available: {available}") from None

    def __iter__(self) -> Iterator[DeviceHandle]:
        return iter(list(self._handles.values()))

    def call(
        self, fn: Callable[..., Any], *args: Any, timeout: float = CALL_TIMEOUT, **kwargs: Any
    ) -> Any:
        """Run ``fn`` on the simulator's event loop and return its result.

        Coroutine results are awaited. Exceptions are re-raised in the caller.
        """
        loop = self._loop
        if loop is None or not self.running:
            raise RuntimeError("simulator is not running")
        if threading.current_thread() is self._thread:
            raise RuntimeError("call() would deadlock: already on the simulator thread")

        async def runner() -> Any:
            result = fn(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            return result

        return asyncio.run_coroutine_threadsafe(runner(), loop).result(timeout)

    # ------------------------------------------------------------------ time

    def time(self) -> float:
        """Current simulation time (seconds; virtual time starts at 0)."""
        return float(self.call(self._clock.time))

    def advance(self, seconds: float) -> None:
        """Move the virtual clock forward, running everything that falls due."""
        if not isinstance(self._clock, VirtualClock):
            raise RuntimeError("advance() needs a virtual clock: use clock='virtual'")
        self.call(self._clock.advance, seconds, timeout=CALL_TIMEOUT + seconds / 100)

    async def advance_async(self, seconds: float) -> None:
        """:meth:`advance` for async tests: does not block the caller's event loop."""
        await asyncio.to_thread(self.advance, seconds)


class FaultHandle:
    """Thread-safe view of an :class:`~fieldsim.core.faults.ActiveFault`."""

    def __init__(self, sim: BackgroundSimulator, fault: ActiveFault):
        self._sim = sim
        self._fault = fault

    @property
    def kind(self) -> str:
        """Fault type, e.g. ``"latency"``."""
        return self._fault.kind

    @property
    def hits(self) -> int:
        """How many requests this fault affected so far."""
        return int(self._sim.call(lambda: self._fault.hits))

    @property
    def active(self) -> bool:
        return bool(self._sim.call(lambda: self._fault.active))

    def remove(self) -> None:
        if self._sim.running:
            self._sim.call(self._fault.remove)

    def __enter__(self) -> FaultHandle:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.remove()

    def __repr__(self) -> str:
        return f"<FaultHandle {self.kind}>"


class DeviceHandle:
    """Thread-safe remote control for one device of a :class:`BackgroundSimulator`."""

    def __init__(self, sim: BackgroundSimulator, device: Device[Any]):
        self.simulator = sim
        #: The underlying device. Only touch it from the simulator thread.
        self.device = device
        self.name = device.name
        self.protocol = device.protocol

    # Connection details are fixed once the device is running, so plain reads are safe.
    @property
    def host(self) -> str:
        return self.device.host

    @property
    def port(self) -> int:
        return self.device.port

    @property
    def address(self) -> tuple[str, int]:
        return self.device.address

    @property
    def unit_id(self) -> int:
        """Modbus unit id clients should use."""
        return int(getattr(self.device, "unit_id", 1))

    @property
    def endpoint(self) -> str:
        """OPC UA endpoint URL, e.g. ``opc.tcp://127.0.0.1:4840/``."""
        if not hasattr(self.device, "endpoint"):
            raise AttributeError(f"{self.protocol} devices have no OPC UA endpoint")
        return str(self.device.endpoint)

    def node_id(self, point: str) -> str:
        """OPC UA node id string of a point, e.g. ``"ns=2;s=Line1.Speed"``."""
        if not hasattr(self.device, "node_id"):
            raise AttributeError(f"{self.protocol} devices have no OPC UA node ids")
        return str(self._call(self.device.node_id, point))

    def _call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return self.simulator.call(fn, *args, **kwargs)  # type: ignore[no-any-return]

    # ------------------------------------------------------------------ values

    def get(self, name: str) -> Value:
        return self._call(self.device.get, name)

    def set(self, name: str, value: Value) -> None:
        self._call(self.device.set, name, value)

    def release(self, name: str) -> None:
        self._call(self.device.release, name)

    # ------------------------------------------------------------------ faults

    def fault(self, fault_type: str, **params: Any) -> FaultHandle:
        """Activate a fault; use the result as a context manager to scope it."""
        active = self._call(self.device.fault, fault_type, **params)
        return FaultHandle(self.simulator, active)

    def clear_faults(self) -> None:
        self._call(self.device.clear_faults)

    # ------------------------------------------------------------------ outages

    @property
    def online(self) -> bool:
        return bool(self._call(lambda: self.device.online))

    def go_offline(self) -> None:
        self.simulator.call(self.device.go_offline)

    def go_online(self) -> None:
        self.simulator.call(self.device.go_online)

    @contextmanager
    def offline(self) -> Iterator[None]:
        """``with device.offline(): ...`` - outage for the duration of the block."""
        self.go_offline()
        try:
            yield
        finally:
            if self.simulator.running:
                self.go_online()

    # ------------------------------------------------------------------ writes

    @property
    def writes(self) -> list[Any]:
        """Snapshot of every client write recorded so far."""
        return self._call(lambda: list(self.device.writes))

    def clear_writes(self) -> None:
        self._call(self.device.clear_writes)

    def wait_for_write(self, point: str | None = None, timeout: float = 5.0) -> Any:
        """Block until the next client write (touching ``point``).

        See :meth:`fieldsim.Device.wait_for_write`.
        """
        return self.simulator.call(self.device.wait_for_write, point, timeout, timeout=timeout + 5)

    async def wait_for_write_async(self, point: str | None = None, timeout: float = 5.0) -> Any:
        """:meth:`wait_for_write` for async tests: does not block the caller's event loop."""
        return await asyncio.to_thread(self.wait_for_write, point, timeout)

    # ------------------------------------------------------------------ scenario

    def advance(self, seconds: float) -> None:
        """Shortcut for ``simulator.advance`` (the clock is shared by all devices)."""
        self.simulator.advance(seconds)

    async def advance_async(self, seconds: float) -> None:
        await self.simulator.advance_async(seconds)

    @property
    def scenario_done(self) -> bool:
        return bool(self._call(lambda: self.device.scenario.done))

    def __repr__(self) -> str:
        return f"<DeviceHandle {self.name!r} {self.protocol} {self.host}:{self.port}>"
