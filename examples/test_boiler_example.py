"""How a project would test its own Modbus code with fieldsim.

``read_temperature`` and ``set_setpoint`` stand in for the code under test.
Run with:  pytest examples/
"""

from __future__ import annotations

import pytest
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

pytestmark = pytest.mark.fieldsim("boiler.yaml")


# --------------------------------------------------------------------------- code under test


def read_temperature(host: str, port: int, *, timeout: float = 1.0, retries: int = 0) -> float:
    with ModbusTcpClient(host, port=port, timeout=timeout, retries=retries) as client:
        result = client.read_holding_registers(0, count=2, device_id=1)
        if result.isError():
            raise ModbusException(f"device answered with {result}")
        value = client.convert_from_registers(result.registers, client.DATATYPE.FLOAT32)
        return float(value)  # type: ignore[arg-type]


def set_setpoint(host: str, port: int, value: int) -> None:
    with ModbusTcpClient(host, port=port, timeout=1, retries=0) as client:
        client.write_register(2, value, device_id=1)


# --------------------------------------------------------------------------- tests


def test_reads_the_temperature(modbus_device):
    modbus_device.set("temperature", 85.5)
    assert read_temperature(*modbus_device.address) == 85.5


def test_temperature_follows_the_generator(modbus_device):
    assert 60 <= read_temperature(*modbus_device.address) <= 90


def test_writes_the_setpoint(modbus_device):
    set_setpoint(*modbus_device.address, 80)
    modbus_device.wait_for_write("setpoint", timeout=2)
    assert modbus_device.get("setpoint") == 80


def test_busy_device_is_reported(modbus_device):
    with modbus_device.fault("exception", code=6), pytest.raises(ModbusException):
        read_temperature(*modbus_device.address)


def test_one_lost_response_is_retried(modbus_device):
    with modbus_device.fault("drop", count=1) as fault:
        assert read_temperature(*modbus_device.address, timeout=0.3, retries=1) is not None
    assert fault.hits == 1


def test_outage_is_reported(modbus_device):
    with modbus_device.offline(), pytest.raises(ModbusException):
        read_temperature(*modbus_device.address, timeout=0.3)
    assert read_temperature(*modbus_device.address) is not None


@pytest.mark.fieldsim("boiler.yaml", clock="virtual")
def test_overheat_scenario(modbus_device):
    modbus_device.advance(29)
    assert read_temperature(*modbus_device.address) < 100
    modbus_device.advance(1)
    assert read_temperature(*modbus_device.address) == 120.0
