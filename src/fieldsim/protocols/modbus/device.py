"""Modbus TCP device: datastore + generators + server."""

from __future__ import annotations

from fieldsim.config import ModbusTcpDevice
from fieldsim.core.device import Device
from fieldsim.core.generators import build_generator
from fieldsim.protocols.modbus.datastore import ModbusDataStore
from fieldsim.protocols.modbus.server import ModbusTcpServer


class ModbusDevice(Device[ModbusDataStore]):
    """A virtual Modbus TCP device built from a :class:`ModbusTcpDevice` spec."""

    protocol = "modbus_tcp"

    def __init__(self, spec: ModbusTcpDevice):
        store = ModbusDataStore(spec.registers, unmapped=spec.unmapped)
        generators = {
            point.name: build_generator(point.generator)
            for point in spec.registers.all_points()
            if point.generator is not None
        }
        super().__init__(spec.name, store, generators, update_interval=spec.update_interval)
        self.spec = spec
        self.server = ModbusTcpServer(store, host=spec.host, port=spec.port, unit_id=spec.unit_id)

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
