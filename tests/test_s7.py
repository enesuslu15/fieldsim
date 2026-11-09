"""S7 devices end to end, verified with the python-snap7 client."""

from __future__ import annotations

import asyncio
import random
import struct
import time

import pytest
from snap7.client import Client

from fieldsim.config import S7DeviceSpec
from fieldsim.core.clock import VirtualClock
from fieldsim.protocols.s7 import S7DataStore, S7Device
from fieldsim.protocols.s7.proxy import TPKT, FramingError, corrupt, read_tpkt, s7_header

POINTS = [
    {"name": "pressure", "offset": 0, "type": "REAL", "value": 200.5},
    {"name": "cycles", "offset": 4, "type": "DINT", "value": -3},
    {"name": "running", "offset": 8, "bit": 0, "type": "BOOL", "value": True},
    {"name": "door", "offset": 8, "bit": 1, "type": "BOOL"},
    {"name": "setpoint", "offset": 10, "type": "INT", "value": 200},
    {"name": "total", "offset": 12, "type": "LREAL", "value": 1.25},
    {"name": "mode", "offset": 20, "type": "BYTE", "value": 7},
    {"name": "flags", "offset": 22, "type": "WORD", "value": 0xBEEF},
]


def spec(**overrides) -> S7DeviceSpec:
    data = {
        "name": "press",
        "protocol": "s7",
        "port": 0,
        "update_interval": "20ms",
        "data_blocks": [{"number": 1, "points": POINTS}],
        "merkers": {
            "size": 16,
            "points": [{"name": "alarm", "offset": 2, "bit": 3, "type": "BOOL"}],
        },
        "inputs": {"points": [{"name": "sensor", "offset": 0, "type": "WORD", "value": 9}]},
        "outputs": {"points": [{"name": "lamp", "offset": 1, "bit": 7, "type": "BOOL"}]},
    }
    data.update(overrides)
    return S7DeviceSpec.model_validate(data)


@pytest.fixture
async def device():
    async with S7Device(spec()) as dev:
        yield dev


def connect(device, timeout: float = 0.5) -> Client:
    client = Client()
    client.connect(device.host, device.rack, device.slot, device.port)
    client.connection.socket.settimeout(timeout)  # snap7's default is 5 s
    return client


def read_real(device, db: int = 1, offset: int = 0) -> float:
    client = connect(device)
    try:
        return float(struct.unpack(">f", client.db_read(db, offset, 4))[0])
    finally:
        client.disconnect()


# --------------------------------------------------------------------------- data store


def test_store_layout_and_values():
    store = S7DataStore(spec().areas())
    assert store.get("pressure") == 200.5
    assert store.get("cycles") == -3
    assert store.get("running") is True
    assert store.get("door") is False
    assert store.get("total") == 1.25
    assert store.get("flags") == 0xBEEF
    assert store.buffers[("DB", 1)][8] == 0b01
    store.set("door", True)
    store.set("running", False)
    assert store.buffers[("DB", 1)][8] == 0b10
    store.set("setpoint", 70000, clamp=True)
    assert store.get("setpoint") == 32767
    with pytest.raises(ValueError, match="out of range for INT"):
        store.set("setpoint", 70000)
    with pytest.raises(TypeError, match="got a string"):
        store.set("mode", "x")
    assert store.points_in("DB", 1, 8, 1) == ("running", "door")
    assert store.points_in("DB", 1, 2, 4) == ("pressure", "cycles")
    assert store.points_in("MK", 0, 0, 16) == ("alarm",)
    with pytest.raises(KeyError, match="no point named"):
        store.get("nope")


@pytest.mark.parametrize(
    ("name", "address"),
    [
        ("pressure", "DB1.DBD0"),
        ("running", "DB1.DBX8.0"),
        ("setpoint", "DB1.DBW10"),
        ("mode", "DB1.DBB20"),
        ("total", "DB1.DBD12"),
        ("alarm", "M2.3"),
        ("sensor", "IW0"),
        ("lamp", "Q1.7"),
    ],
)
def test_siemens_addresses(name, address):
    assert S7DataStore(spec().areas()).point(name).address == address


def test_merker_byte_address():
    area = {"size": 4, "points": [{"name": "b", "offset": 3, "type": "BYTE"}]}
    assert S7DataStore(spec(merkers=area).areas()).point("b").address == "MB3"


# --------------------------------------------------------------------------- over the wire


async def test_client_reads_all_areas(device):
    def work():
        c = connect(device)
        try:
            return (
                struct.unpack(">f", c.db_read(1, 0, 4))[0],
                c.db_read(1, 8, 1)[0],
                struct.unpack(">H", c.eb_read(0, 2))[0],
            )
        finally:
            c.disconnect()

    assert await asyncio.to_thread(work) == (200.5, 1, 9)


async def test_api_changes_reach_clients(device):
    device.set("pressure", 180.25)
    assert await asyncio.to_thread(read_real, device) == 180.25


async def test_client_writes_are_recorded(device):
    def work():
        c = connect(device)
        try:
            c.db_write(1, 10, bytearray(struct.pack(">h", -42)))
            c.mb_write(2, 1, bytearray([0b1000]))
        finally:
            c.disconnect()

    await asyncio.to_thread(work)
    event = await device.wait_for_write("setpoint", timeout=2)
    assert (event.area, event.number, event.start, event.size) == ("DB", 1, 10, 2)
    assert event.values == (-42,)
    event = await device.wait_for_write("alarm", timeout=2)
    assert event.area == "MK"
    assert device.get("alarm") is True


async def test_write_outside_points_is_recorded_without_points():
    async with S7Device(spec(data_blocks=[{"number": 1, "size": 64, "points": POINTS}])) as device:

        def work():
            c = connect(device)
            try:
                c.db_write(1, 30, bytearray(b"\x01"))
            finally:
                c.disconnect()

        await asyncio.to_thread(work)
        event = await device.wait_for_write(timeout=2)
        assert event.points == ()
        assert (event.start, event.size) == (30, 1)


async def test_generator_changes_the_value():
    points = [
        {
            "name": "level",
            "offset": 0,
            "type": "REAL",
            "generator": {"type": "ramp", "start": 0, "end": 1000, "period": 1},
        },
    ]
    async with S7Device(spec(data_blocks=[{"number": 2, "points": points}])) as device:
        first = await asyncio.to_thread(read_real, device, 2)
        await asyncio.sleep(0.15)
        assert await asyncio.to_thread(read_real, device, 2) > first


async def test_virtual_clock_scenario():
    clock = VirtualClock()
    scenario = [{"at": 10, "set": {"running": False, "pressure": 0}}]
    async with S7Device(spec(scenario=scenario), clock=clock) as device:
        await clock.advance(10)
        assert device.get("running") is False
        assert await asyncio.to_thread(read_real, device) == 0


# --------------------------------------------------------------------------- faults


async def test_latency(device):
    with device.fault("latency", delay="250ms", services=["Read"]):
        start = time.perf_counter()
        await asyncio.to_thread(read_real, device)
        assert time.perf_counter() - start >= 0.2  # Windows timers may fire early


async def test_write_filter_does_not_slow_reads(device):
    with device.fault("latency", delay=1, services=["Write"]):
        start = time.perf_counter()
        await asyncio.to_thread(read_real, device)
        assert time.perf_counter() - start < 0.9


async def test_drop_then_recover(device):
    client = await asyncio.to_thread(connect, device)
    try:
        with device.fault("drop", services=["Read"], count=1) as fault:
            with pytest.raises(Exception):  # noqa: B017, PT011 - snap7 raises its own errors
                await asyncio.wait_for(asyncio.to_thread(client.db_read, 1, 0, 4), 10)
            assert fault.hits == 1
    finally:
        await asyncio.to_thread(client.disconnect)
    assert await asyncio.to_thread(read_real, device) == 200.5


async def test_drop_executed_write(device):
    def work():
        c = connect(device)
        try:
            c.db_write(1, 10, bytearray(struct.pack(">h", 5)))
        finally:
            c.disconnect()

    with (
        device.fault("drop", executed=True, services=["Write"], count=1),
        pytest.raises(Exception),  # noqa: B017, PT011
    ):
        await asyncio.wait_for(asyncio.to_thread(work), 10)
    assert device.get("setpoint") == 5


async def test_disconnect(device):
    with (
        device.fault("disconnect", services=["Read"], count=1),
        pytest.raises(Exception),  # noqa: B017, PT011
    ):
        await asyncio.to_thread(read_real, device)
    assert await asyncio.to_thread(read_real, device) == 200.5


@pytest.mark.parametrize("mode", ["truncate", "garbage", "bad_length"])
async def test_corrupt_breaks_the_read(device, mode):
    with (
        device.fault("corrupt", mode=mode, services=["Read"], count=1),
        pytest.raises(Exception),  # noqa: B017, PT011
    ):
        await asyncio.wait_for(asyncio.to_thread(read_real, device), 10)


async def test_fault_validation(device):
    with pytest.raises(ValueError, match="not supported for S7"):
        device.fault("status")
    with pytest.raises(ValueError, match="does not exist for S7"):
        device.fault("corrupt", mode="bad_crc")


# --------------------------------------------------------------------------- lifecycle


async def test_offline_and_back(device):
    async with device.offline():
        with pytest.raises(OSError):  # noqa: PT011
            await asyncio.open_connection(device.host, device.port)
        device.set("pressure", 1.5)
    assert await asyncio.to_thread(read_real, device) == 1.5


async def test_restart_keeps_values():
    device = S7Device(spec())
    await device.start()
    device.set("pressure", 4.5)
    await device.stop()
    assert device.server is None
    await device.start()
    try:
        assert await asyncio.to_thread(read_real, device) == 4.5
    finally:
        await device.stop()


async def test_port_in_use():
    async with S7Device(spec()) as first:
        with pytest.raises(OSError):  # noqa: PT011
            await S7Device(spec(port=first.port)).start()


# --------------------------------------------------------------------------- framing


def packet(s7: bytes) -> bytes:
    body = b"\x02\xf0\x80" + s7
    return TPKT.pack(3, 0, TPKT.size + len(body)) + body


READ_JOB = packet(bytes.fromhex("320100000a0b000e00000401120a10020004000184000000"))


def test_s7_header():
    header = s7_header(READ_JOB)
    assert header is not None
    assert (header.rosctr, header.pdu_ref, header.service) == (1, 0x0A0B, "Read")
    assert s7_header(TPKT.pack(3, 0, 11) + b"\x06\xe0\x00\x00\x00\x01\x00") is None  # COTP CR
    assert s7_header(packet(b"\x72\x01")) is None  # not S7comm
    assert s7_header(b"\x03\x00") is None


async def test_read_tpkt():
    reader = asyncio.StreamReader()
    reader.feed_data(READ_JOB)
    reader.feed_eof()
    assert bytes(await read_tpkt(reader)) == READ_JOB
    bad = asyncio.StreamReader()
    bad.feed_data(b"\x04\x00\x00\x10")
    bad.feed_eof()
    with pytest.raises(FramingError):
        await read_tpkt(bad)


@pytest.mark.parametrize("mode", ["truncate", "garbage", "bad_length", "bit_flip"])
def test_corrupt_modes(mode):
    damaged = corrupt(READ_JOB, mode, random.Random(2))
    assert damaged != READ_JOB
    if mode == "bit_flip":
        assert damaged[:8] == READ_JOB[:8]  # TPKT, COTP and protocol id untouched
    with pytest.raises(ValueError, match="not supported for S7"):
        corrupt(READ_JOB, "bad_crc", random.Random())
