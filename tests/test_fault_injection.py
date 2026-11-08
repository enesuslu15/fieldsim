"""Faults, outages and RTU framing, observed through a real client."""

import asyncio
import time

import pytest
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException
from pymodbus.framer import FramerType

from fieldsim.protocols.modbus import ModbusDevice

from .conftest import modbus_spec


async def connect(device, *, timeout=0.5, framer=FramerType.SOCKET):
    client = AsyncModbusTcpClient(
        device.host, port=device.port, timeout=timeout, retries=0, framer=framer, reconnect_delay=0
    )
    assert await client.connect()
    return client


async def test_latency(device, client):
    with device.fault("latency", delay="300ms"):
        start = time.perf_counter()
        await client.read_holding_registers(0)
        assert time.perf_counter() - start >= 0.29
    start = time.perf_counter()
    await client.read_holding_registers(0)
    assert time.perf_counter() - start < 0.29  # generous: CI runners can be slow


async def test_drop_times_out_and_counts(device):
    client = await connect(device, timeout=0.2)
    try:
        with device.fault("drop", count=1) as fault:
            with pytest.raises(ModbusException):
                await client.read_holding_registers(0)
            assert fault.hits == 1
            assert not fault.active
    finally:
        client.close()


async def test_drop_executed_applies_the_write(device):
    client = await connect(device, timeout=0.2)
    try:
        with (
            device.fault("drop", executed=True, function_codes=[6]),
            pytest.raises(ModbusException),
        ):
            await client.write_register(0, 1234)
        assert device.get("setpoint") == 1234
    finally:
        client.close()


async def test_exception_fault(device, client):
    with device.fault("exception", code=6):
        result = await client.read_holding_registers(0)
    assert result.isError()
    assert result.exception_code == 6


async def test_disconnect(device):
    reader, writer = await asyncio.open_connection(device.host, device.port)
    device.fault("disconnect", count=1)
    writer.write(b"\x00\x01\x00\x00\x00\x06\x01\x03\x00\x00\x00\x01")
    assert await reader.read() == b""
    writer.close()


async def test_corrupt_transaction_id_is_rejected_by_client(device):
    client = await connect(device, timeout=0.2)
    try:
        with device.fault("corrupt", mode="transaction_id"), pytest.raises(ModbusException):
            await client.read_holding_registers(0)
    finally:
        client.close()


async def test_corrupt_truncate_raw(device):
    reader, writer = await asyncio.open_connection(device.host, device.port)
    with device.fault("corrupt", mode="truncate"):
        writer.write(b"\x00\x01\x00\x00\x00\x06\x01\x03\x00\x00\x00\x01")
        data = await asyncio.wait_for(reader.read(100), 1)
    assert 0 < len(data) < 11
    writer.close()


async def test_fault_validation_per_protocol(device):
    with pytest.raises(ValueError, match="needs RTU framing"):
        device.fault("corrupt", mode="bad_crc")


async def test_faults_from_config():
    spec = modbus_spec(faults=[{"type": "exception", "code": 4, "function_codes": [3]}], seed=1)
    async with ModbusDevice(spec) as device:
        client = await connect(device)
        try:
            assert (await client.read_holding_registers(0)).exception_code == 4
            assert not (await client.read_coils(0)).isError()
        finally:
            client.close()
        device.clear_faults()
        assert device.faults.active == []


# --------------------------------------------------------------------------- outages


async def test_offline_refuses_connections_then_recovers():
    async with ModbusDevice(modbus_spec()) as device:
        port = device.port
        client = await connect(device)
        async with device.offline():
            assert not device.online
            assert "offline" in repr(device)
            with pytest.raises(OSError):  # noqa: PT011
                await asyncio.open_connection(device.host, port)
            device.set("setpoint", 5)  # values still change while offline
        assert device.online
        assert device.port == port
        client.close()
        client = await connect(device)
        try:
            assert (await client.read_holding_registers(0)).registers == [5]
        finally:
            client.close()


async def test_offline_requires_running_device():
    device = ModbusDevice(modbus_spec())
    with pytest.raises(RuntimeError, match="not running"):
        await device.go_offline()


async def test_offline_and_online_are_idempotent(device):
    await device.go_online()
    await device.go_offline()
    await device.go_offline()
    await device.stop()  # stopping while offline must not try to stop the server twice
    assert not device.running


# --------------------------------------------------------------------------- write log


async def test_wait_for_write(device, client):
    await client.write_register(0, 11)
    await client.write_coil(1, True)
    event = await device.wait_for_write("valve", timeout=1)
    assert event.points == ("valve",)
    # the setpoint write happened before the valve write and was skipped over
    with pytest.raises(TimeoutError, match="no write to 'setpoint'"):
        await device.wait_for_write("setpoint", timeout=0.05)

    async def later():
        await asyncio.sleep(0.05)
        await client.write_register(0, 12)

    task = asyncio.create_task(later())
    event = await device.wait_for_write(timeout=1)
    assert event.values == (12,)
    await task
    assert len(device.writes) == 3
    device.clear_writes()
    assert device.writes == []


async def test_wait_for_write_unknown_point(device):
    with pytest.raises(KeyError):
        await device.wait_for_write("nope")


# --------------------------------------------------------------------------- RTU over TCP


@pytest.fixture
async def rtu_device():
    async with ModbusDevice(modbus_spec(protocol="modbus_rtu_over_tcp", unit_id=1)) as dev:
        yield dev


async def test_rtu_over_tcp_with_pymodbus(rtu_device):
    client = await connect(rtu_device, framer=FramerType.RTU)
    try:
        assert (await client.read_holding_registers(0)).registers == [75]
        await client.write_registers(0, [42])
        assert rtu_device.get("setpoint") == 42
        assert (await client.read_coils(0, count=3)).bits[:3] == [True, False, False]
    finally:
        client.close()


async def test_rtu_bad_crc_fault(rtu_device):
    client = await connect(rtu_device, framer=FramerType.RTU, timeout=0.2)
    try:
        with (
            rtu_device.fault("corrupt", mode="bad_crc", count=1),
            pytest.raises(ModbusException),
        ):
            await client.read_holding_registers(0)
    finally:
        client.close()


async def test_rtu_rejects_tcp_only_fault(rtu_device):
    with pytest.raises(ValueError, match="only exists in Modbus TCP"):
        rtu_device.fault("corrupt", mode="transaction_id")


async def test_rtu_ignores_frames_with_bad_crc(rtu_device):
    reader, writer = await asyncio.open_connection(rtu_device.host, rtu_device.port)
    writer.write(b"\x01\x03\x00\x00\x00\x01\x00\x00")  # wrong CRC
    writer.write(b"\x01\x03\x00\x00\x00\x01\x84\x0a")  # good
    response = await asyncio.wait_for(reader.readexactly(7), 1)
    assert response[:5] == b"\x01\x03\x02\x00\x4b"
    writer.close()
