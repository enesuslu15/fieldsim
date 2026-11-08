"""Modbus device: datastore + generators + faults + scenario + server."""

from __future__ import annotations

import asyncio
import contextlib

from fieldsim.config import CorruptFault, FaultSpec, ModbusDeviceSpec, check_corrupt_mode
from fieldsim.core.clock import Clock
from fieldsim.core.device import Device
from fieldsim.core.generators import build_generator
from fieldsim.protocols.modbus.datastore import ModbusDataStore, WriteEvent
from fieldsim.protocols.modbus.framer import FRAMERS
from fieldsim.protocols.modbus.server import ModbusTcpServer


class ModbusDevice(Device[ModbusDataStore]):
    """A virtual Modbus device built from a :class:`ModbusDeviceSpec`.

    Besides the common :class:`Device` API it records every client write, so a
    test can assert on what the software under test sent::

        event = await device.wait_for_write("setpoint")
        assert device.get("setpoint") == 80
    """

    def __init__(self, spec: ModbusDeviceSpec, *, clock: Clock | None = None):
        self.protocol = spec.protocol
        store = ModbusDataStore(spec.registers, unmapped=spec.unmapped)
        generators = {
            point.name: build_generator(point.generator)
            for point in spec.registers.all_points()
            if point.generator is not None
        }
        super().__init__(
            spec.name,
            store,
            generators,
            update_interval=spec.update_interval,
            faults=spec.faults,
            scenario=spec.scenario,
            seed=spec.seed,
            clock=clock,
        )
        self.spec = spec
        self.server = ModbusTcpServer(
            store,
            host=spec.host,
            port=spec.port,
            unit_id=spec.unit_id,
            framer=FRAMERS[spec.protocol](),
            faults=self.faults,
        )
        self.writes: list[WriteEvent] = []
        self._write_cursor = 0
        self._write_signal = asyncio.Event()
        store.add_write_listener(self._on_write)

    @property
    def host(self) -> str:
        return self.server.host

    @property
    def port(self) -> int:
        return self.server.port

    @property
    def unit_id(self) -> int:
        """Unit id clients should use (1 when the device answers every unit id)."""
        return 1 if self.spec.unit_id is None else self.spec.unit_id

    async def _start_server(self) -> None:
        await self.server.start()

    async def _stop_server(self) -> None:
        await self.server.stop()

    def validate_fault(self, spec: FaultSpec) -> None:
        if isinstance(spec, CorruptFault):
            check_corrupt_mode(spec.mode, self.protocol)

    # ------------------------------------------------------------------ write log

    def _on_write(self, event: WriteEvent) -> None:
        self.writes.append(event)
        self._write_signal.set()

    def clear_writes(self) -> None:
        self.writes.clear()
        self._write_cursor = 0

    async def wait_for_write(self, point: str | None = None, timeout: float = 5.0) -> WriteEvent:
        """Wait for a client write (touching ``point``, if given) and return it.

        Writes are consumed in order: each call returns the next matching write
        that no earlier call has returned, including writes that happened before
        the call. Raises :class:`TimeoutError` if none arrives in time.
        """
        if point is not None:
            self.store.point(point)  # validates the name
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
