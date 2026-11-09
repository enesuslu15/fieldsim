"""Runs every device of a simulation file together."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Any

from fieldsim.config import (
    AnyDeviceSpec,
    ModbusDeviceSpec,
    OpcUaDeviceSpec,
    S7DeviceSpec,
    SimulationConfig,
    load_config,
    parse_config,
)
from fieldsim.core.clock import Clock, RealClock
from fieldsim.core.device import Device


def create_device(spec: AnyDeviceSpec, *, clock: Clock | None = None) -> Device[Any]:
    """Instantiate the device class matching ``spec.protocol``."""
    if isinstance(spec, ModbusDeviceSpec):
        from fieldsim.protocols.modbus.device import ModbusDevice

        return ModbusDevice(spec, clock=clock)
    if isinstance(spec, OpcUaDeviceSpec):
        from fieldsim.protocols.opcua.device import OpcUaDevice

        return OpcUaDevice(spec, clock=clock)
    if isinstance(spec, S7DeviceSpec):
        from fieldsim.protocols.s7.device import S7Device

        return S7Device(spec, clock=clock)
    raise TypeError(f"unsupported device spec: {type(spec).__name__}")  # pragma: no cover


class Simulator:
    """A group of devices started and stopped as a unit.

    ::

        async with Simulator.from_yaml("plant.yaml") as sim:
            plc = sim["boiler-plc"]
            plc.set("temperature", 85.0)
    """

    def __init__(self, config: SimulationConfig, *, clock: Clock | None = None):
        self.config = config
        #: Shared by every device, so their scenarios stay in step.
        self.clock: Clock = clock or RealClock()
        self.devices: dict[str, Device[Any]] = {
            spec.name: create_device(spec, clock=self.clock) for spec in config.devices
        }

    @classmethod
    def from_yaml(cls, path: str | Path, *, clock: Clock | None = None) -> Simulator:
        return cls(load_config(path), clock=clock)

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, clock: Clock | None = None) -> Simulator:
        return cls(parse_config(data), clock=clock)

    def __getitem__(self, name: str) -> Device[Any]:
        try:
            return self.devices[name]
        except KeyError:
            raise KeyError(
                f"no device named {name!r}; available: {', '.join(self.devices)}"
            ) from None

    def __iter__(self) -> Iterator[Device[Any]]:
        return iter(self.devices.values())

    def __len__(self) -> int:
        return len(self.devices)

    async def start(self) -> None:
        """Start all devices. If one fails, the ones already started are stopped again."""
        started: list[Device[Any]] = []
        try:
            for device in self.devices.values():
                await device.start()
                started.append(device)
        except BaseException:
            for device in reversed(started):
                await device.stop()
            raise

    async def stop(self) -> None:
        for device in reversed(list(self.devices.values())):
            await device.stop()

    async def __aenter__(self) -> Simulator:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()
