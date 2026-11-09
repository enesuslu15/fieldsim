"""Testing OPC UA client code against a simulated line. Run with:  pytest examples/"""

from __future__ import annotations

import asyncio

import pytest
from asyncua import Client, ua
from asyncua.ua.uaerrors import BadTimeout

pytestmark = pytest.mark.fieldsim("packaging_line.yaml")


# --------------------------------------------------------------------------- code under test


async def read_speed(endpoint: str, node_id: str) -> float:
    async with Client(endpoint, timeout=2) as client:
        return float(await client.get_node(node_id).read_value())


async def change_product(endpoint: str, node_id: str, product: str) -> None:
    async with Client(endpoint, timeout=2) as client:
        await client.get_node(node_id).write_value(ua.Variant(product, ua.VariantType.String))


# --------------------------------------------------------------------------- tests


async def test_reads_the_speed(opcua_device):
    opcua_device.set("Line1.Speed", 1.05)
    speed = await read_speed(opcua_device.endpoint, opcua_device.node_id("Line1.Speed"))
    assert speed == 1.05


async def test_changes_the_product(opcua_device):
    await change_product(opcua_device.endpoint, opcua_device.node_id("Line1.Product"), "B-200")
    event = await opcua_device.wait_for_write_async("Line1.Product")
    assert event.values == ("B-200",)
    assert opcua_device.get("Line1.Product") == "B-200"


async def test_timeouts_are_reported(opcua_device):
    with (
        opcua_device.fault("status", code="BadTimeout", services=["Read"]),
        pytest.raises(BadTimeout),
    ):
        await read_speed(opcua_device.endpoint, opcua_device.node_id("Line1.Speed"))


async def test_slow_server_still_answers(opcua_device):
    with opcua_device.fault("latency", delay="300ms", services=["Read"]):
        loop = asyncio.get_running_loop()
        start = loop.time()
        await read_speed(opcua_device.endpoint, opcua_device.node_id("Line1.Speed"))
        assert loop.time() - start >= 0.3


@pytest.mark.fieldsim("packaging_line.yaml", clock="virtual")
async def test_jam_scenario(opcua_device):
    await opcua_device.advance_async(30)
    assert opcua_device.get("Line1.Running") is False
    assert opcua_device.get("Status") == "STOPPED: jam"
