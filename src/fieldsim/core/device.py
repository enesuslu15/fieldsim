"""Base class shared by every simulated device."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from types import TracebackType
from typing import Generic, TypeVar

from fieldsim.core.datastore import PointStore, Value
from fieldsim.core.engine import GeneratorEngine
from fieldsim.core.generators import Generator

StoreT = TypeVar("StoreT", bound=PointStore)


class Device(ABC, Generic[StoreT]):
    """A running (or startable) virtual device.

    Subclasses only implement the network side (:meth:`_start_server`,
    :meth:`_stop_server`, :attr:`host`, :attr:`port`); value handling and
    generators are shared.
    """

    protocol: str = ""

    def __init__(
        self,
        name: str,
        store: StoreT,
        generators: Mapping[str, Generator],
        *,
        update_interval: float,
    ):
        self.name = name
        self.store = store
        self._engine = GeneratorEngine(store, generators, interval=update_interval)
        self._running = False

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
    async def _stop_server(self) -> None: ...

    # ------------------------------------------------------------------ lifecycle

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        if self._running:
            return
        await self._start_server()
        self._engine.start()
        self._running = True

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        await self._engine.stop()
        await self._stop_server()

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
        state = "running" if self._running else "stopped"
        return f"<{type(self).__name__} {self.name!r} {self.host}:{self.port} {state}>"
