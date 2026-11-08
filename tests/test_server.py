"""End-to-end tests over real sockets.

pymodbus is used as an independent client implementation so we are not only
testing our server against our own understanding of the protocol.
"""

import asyncio
import struct

import pytest
from pymodbus.client import AsyncModbusTcpClient

from fieldsim.core.types import decode
from fieldsim.protocols.modbus import ModbusDevice, ModbusTcpServer

from .conftest import modbus_spec

# --------------------------------------------------------------------------- pymodbus client


async def test_read_all_tables(client):
    coils = await client.read_coils(0, count=3)
    assert coils.bits[:3] == [True, False, False]
    di = await client.read_discrete_inputs(0, count=1)
    assert di.bits[0] is True
    hr = await client.read_holding_registers(0, count=3)
    assert hr.registers[0] == 75
    assert decode(hr.registers[1:3], "float32") == 21.5
    ir = await client.read_input_registers(0, count=2)
    assert decode(ir.registers, "uint32") == 70000


async def test_writes_are_visible_through_the_api(device, client):
    await client.write_coil(1, True)
    await client.write_coils(0, [False, False])
    await client.write_register(0, 99)
    await client.write_registers(1, [0xC2C8, 0x0000])  # -100.0
    assert device.get("valve") is False
    assert device.get("pump") is False
    assert device.get("setpoint") == 99
    assert device.get("temperature") == -100.0


async def test_api_changes_are_visible_to_the_client(device, client):
    device.set("setpoint", 4321)
    result = await client.read_holding_registers(0, count=1)
    assert result.registers == [4321]


async def test_exception_response(client):
    result = await client.read_holding_registers(500, count=1)
    assert result.isError()
    assert result.exception_code == 2


async def test_multiple_clients(device):
    clients = [AsyncModbusTcpClient(device.host, port=device.port, retries=0) for _ in range(5)]
    try:
        for c in clients:
            assert await c.connect()
        results = await asyncio.gather(*(c.read_holding_registers(0) for c in clients))
        assert [r.registers[0] for r in results] == [75] * 5
        assert device.server.client_count == 5
    finally:
        for c in clients:
            c.close()


# --------------------------------------------------------------------------- raw sockets


def mbap(tid: int, pdu: bytes, unit: int = 1, protocol: int = 0) -> bytes:
    return struct.pack(">HHHB", tid, protocol, len(pdu) + 1, unit) + pdu


async def open_raw(device):
    return await asyncio.open_connection(device.host, device.port)


async def test_transaction_id_and_unit_are_echoed(device):
    reader, writer = await open_raw(device)
    writer.write(mbap(0xBEEF, b"\x03\x00\x00\x00\x01", unit=7))
    response = await reader.readexactly(11)
    assert response == mbap(0xBEEF, b"\x03\x02\x00\x4b", unit=7)
    writer.close()


async def test_pipelined_requests_are_answered_in_order(device):
    reader, writer = await open_raw(device)
    writer.write(mbap(1, b"\x03\x00\x00\x00\x01") + mbap(2, b"\x01\x00\x00\x00\x01"))
    first = await reader.readexactly(11)
    second = await reader.readexactly(10)
    assert struct.unpack(">H", first[:2])[0] == 1
    assert struct.unpack(">H", second[:2])[0] == 2
    writer.close()


async def test_split_frame_is_reassembled(device):
    reader, writer = await open_raw(device)
    frame = mbap(5, b"\x03\x00\x00\x00\x01")
    for byte in frame:
        writer.write(bytes([byte]))
        await writer.drain()
        await asyncio.sleep(0.001)
    assert len(await reader.readexactly(11)) == 11
    writer.close()


@pytest.mark.parametrize(
    "garbage",
    [
        mbap(1, b"\x03\x00\x00\x00\x01", protocol=1),  # not Modbus
        struct.pack(">HHHB", 1, 0, 1, 1),  # length 1: no function code
        struct.pack(">HHHB", 1, 0, 300, 1),  # longer than any Modbus PDU
    ],
)
async def test_invalid_frames_close_the_connection(device, garbage):
    reader, writer = await open_raw(device)
    writer.write(garbage)
    assert await reader.read() == b""
    writer.close()
    # the server is still healthy afterwards
    reader, writer = await open_raw(device)
    writer.write(mbap(1, b"\x03\x00\x00\x00\x01"))
    assert len(await reader.readexactly(11)) == 11
    writer.close()


async def test_unit_id_filter():
    async with ModbusDevice(modbus_spec(unit_id=3)) as device:
        assert device.unit_id == 3
        reader, writer = await open_raw(device)
        writer.write(mbap(1, b"\x03\x00\x00\x00\x01", unit=9))  # ignored
        writer.write(mbap(2, b"\x03\x00\x00\x00\x01", unit=3))
        response = await reader.readexactly(11)
        assert struct.unpack(">H", response[:2])[0] == 2
        writer.close()


# --------------------------------------------------------------------------- lifecycle


async def test_port_zero_reports_real_port():
    device = ModbusDevice(modbus_spec())
    assert device.port == 0
    await device.start()
    try:
        assert device.port > 0
        assert device.running
        assert "running" in repr(device)
    finally:
        await device.stop()
    assert not device.running
    assert "stopped" in repr(device)


async def test_start_and_stop_are_idempotent():
    device = ModbusDevice(modbus_spec())
    await device.stop()
    await device.start()
    await device.start()
    await device.stop()
    await device.stop()


async def test_server_cannot_start_twice():
    server = ModbusTcpServer(ModbusDevice(modbus_spec()).store, port=0)
    await server.start()
    try:
        assert server.serving
        with pytest.raises(RuntimeError, match="already started"):
            await server.start()
    finally:
        await server.stop()
    assert not server.serving
    await server.stop()


async def test_stop_disconnects_clients():
    device = ModbusDevice(modbus_spec())
    await device.start()
    reader, writer = await open_raw(device)
    await asyncio.sleep(0.01)
    assert device.server.client_count == 1
    await asyncio.wait_for(device.stop(), timeout=2)
    assert await reader.read() == b""
    writer.close()


async def test_port_in_use_raises_oserror():
    async with ModbusDevice(modbus_spec()) as first:
        clash = ModbusDevice(modbus_spec(port=first.port))
        with pytest.raises(OSError):  # noqa: PT011
            await clash.start()


# --------------------------------------------------------------------------- generators


async def test_generated_values_change_over_the_wire():
    spec = modbus_spec(
        update_interval="10ms",
        registers={
            "holding_registers": [
                {
                    "name": "ramp",
                    "address": 0,
                    "generator": {"type": "ramp", "start": 0, "end": 60000, "period": 1},
                }
            ]
        },
    )
    async with ModbusDevice(spec) as device:
        assert device.generated_points == {"ramp"}
        c = AsyncModbusTcpClient(device.host, port=device.port, retries=0)
        await c.connect()
        try:
            first = (await c.read_holding_registers(0)).registers[0]
            await asyncio.sleep(0.1)
            second = (await c.read_holding_registers(0)).registers[0]
            assert second > first

            device.set("ramp", 7)  # pins the point
            await asyncio.sleep(0.05)
            assert (await c.read_holding_registers(0)).registers[0] == 7

            device.release("ramp")
            await asyncio.sleep(0.05)
            assert (await c.read_holding_registers(0)).registers[0] != 7
        finally:
            c.close()


async def test_release_unknown_point_raises():
    device = ModbusDevice(modbus_spec())
    with pytest.raises(KeyError):
        device.release("nope")
