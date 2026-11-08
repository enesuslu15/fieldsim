from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from pymodbus.client import AsyncModbusTcpClient

from fieldsim.config import ModbusTcpDevice
from fieldsim.protocols.modbus import ModbusDevice

DeviceFactory = Callable[..., Any]


def modbus_spec(**overrides: Any) -> ModbusTcpDevice:
    """A small but representative device with a free port."""
    data: dict[str, Any] = {
        "name": "plc",
        "protocol": "modbus_tcp",
        "port": 0,
        "registers": {
            "coils": [
                {"name": "pump", "address": 0, "value": True},
                {"name": "valve", "address": 1},
                {"name": "locked", "address": 2, "writable": False},
            ],
            "discrete_inputs": [{"name": "door", "address": 0, "value": True}],
            "holding_registers": [
                {"name": "setpoint", "address": 0, "value": 75},
                {"name": "temperature", "address": 1, "type": "float32", "value": 21.5},
                {"name": "readonly", "address": 3, "value": 7, "writable": False},
            ],
            "input_registers": [
                {"name": "counter", "address": 0, "type": "uint32", "value": 70000},
            ],
        },
    }
    data.update(overrides)
    return ModbusTcpDevice.model_validate(data)


@pytest.fixture
async def device() -> AsyncIterator[ModbusDevice]:
    async with ModbusDevice(modbus_spec()) as dev:
        yield dev  # type: ignore[misc]


@pytest.fixture
async def client(device: ModbusDevice) -> AsyncIterator[AsyncModbusTcpClient]:
    c = AsyncModbusTcpClient(device.host, port=device.port, timeout=2, retries=0)
    assert await c.connect()
    yield c
    c.close()
