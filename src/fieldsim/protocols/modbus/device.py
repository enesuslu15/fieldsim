"""Modbus device: datastore + generators + faults + scenario + server."""

from __future__ import annotations

from fieldsim.config import FaultSpec, ModbusDeviceSpec, check_fault
from fieldsim.core.clock import Clock
from fieldsim.core.device import Device
from fieldsim.core.generators import build_generator
from fieldsim.protocols.modbus.datastore import ModbusDataStore
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
        store.add_write_listener(self._record_write)

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
        check_fault(spec, self.protocol)
