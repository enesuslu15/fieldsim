"""Testing S7 client code against a simulated press. Run with:  pytest examples/"""

from __future__ import annotations

import struct

import pytest
from snap7.client import Client

pytestmark = pytest.mark.fieldsim("press.yaml")


# --------------------------------------------------------------------------- code under test


def read_pressure(host: str, port: int, rack: int, slot: int) -> float:
    client = Client()
    client.connect(host, rack, slot, port)
    try:
        return float(struct.unpack(">f", client.db_read(10, 0, 4))[0])
    finally:
        client.disconnect()


def change_setpoint(host: str, port: int, rack: int, slot: int, value: int) -> None:
    client = Client()
    client.connect(host, rack, slot, port)
    try:
        client.db_write(10, 10, bytearray(struct.pack(">h", value)))
    finally:
        client.disconnect()


def plc(device) -> tuple[str, int, int, int]:
    return device.host, device.port, device.rack, device.slot


# --------------------------------------------------------------------------- tests


def test_reads_the_pressure(s7_device):
    s7_device.set("pressure", 201.5)
    assert read_pressure(*plc(s7_device)) == 201.5
    assert s7_device.address_of("pressure") == "DB10.DBD0"


def test_writes_the_setpoint(s7_device):
    change_setpoint(*plc(s7_device), 215)
    event = s7_device.wait_for_write("setpoint", timeout=2)
    assert event.values == (215,)


def test_outage_is_reported(s7_device):
    with s7_device.offline(), pytest.raises(Exception):  # noqa: B017, PT011 - snap7's own errors
        read_pressure(*plc(s7_device))
    assert read_pressure(*plc(s7_device)) > 0


@pytest.mark.fieldsim("press.yaml", clock="virtual")
def test_door_opens_and_raises_the_alarm(s7_device):
    s7_device.advance(20)
    assert s7_device.get("door_open") is True
    assert s7_device.get("alarm") is True
    s7_device.advance(10)
    assert s7_device.get("alarm") is False
