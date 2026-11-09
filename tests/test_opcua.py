"""OPC UA devices end to end, verified with the asyncua client."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest
from asyncua import Client, ua
from asyncua.ua.uaerrors import BadTimeout, UaStatusCodeError

from fieldsim.config import OpcUaDeviceSpec
from fieldsim.core.clock import VirtualClock
from fieldsim.protocols.opcua import OpcUaDataStore, OpcUaDevice

logging.getLogger("asyncua").setLevel(logging.ERROR)

NODES = [
    {
        "name": "Line1",
        "description": "Main line",
        "nodes": [
            {"name": "Speed", "type": "Double", "value": 1.5, "description": "m/s"},
            {"name": "Setpoint", "type": "Int32", "value": 70, "writable": True},
            {"name": "Running", "type": "Boolean", "value": True},
            {"name": "Product", "type": "String", "value": "A", "writable": True},
        ],
    },
    {"name": "Counter", "type": "UInt16", "node_id": "i=1001"},
    {
        "name": "Level",
        "type": "Float",
        "generator": {"type": "ramp", "start": 0, "end": 100, "period": 1},
    },
]


def spec(**overrides) -> OpcUaDeviceSpec:
    data = {
        "name": "line",
        "protocol": "opcua",
        "port": 0,
        "update_interval": "20ms",
        "nodes": NODES,
    }
    data.update(overrides)
    return OpcUaDeviceSpec.model_validate(data)


@pytest.fixture
async def device():
    async with OpcUaDevice(spec()) as dev:
        yield dev


@pytest.fixture
async def client(device):
    async with Client(device.endpoint, timeout=2) as c:
        yield c


def node(client, device, point):
    return client.get_node(device.node_id(point))


# --------------------------------------------------------------------------- data store


def test_store_initial_values_and_types():
    store = OpcUaDataStore(spec().variables())
    assert store.get("Line1.Speed") == 1.5
    assert store.get("Counter") == 0
    assert store.get("Line1.Product") == "A"
    assert store.info("Counter").identifier == "i=1001"
    assert [v.path for v in store.variables()][:2] == ["Line1.Speed", "Line1.Setpoint"]
    store.set("Counter", 70000, clamp=True)
    assert store.get("Counter") == 65535
    with pytest.raises(ValueError, match="out of range"):
        store.set("Counter", -1)
    with pytest.raises(TypeError, match="expected a string"):
        store.set("Line1.Product", 5)
    with pytest.raises(KeyError, match="no point named 'nope'"):
        store.get("nope")


# --------------------------------------------------------------------------- values


async def test_browse_names_and_node_ids(device, client):
    assert device.endpoint.startswith("opc.tcp://127.0.0.1:")
    assert device.node_id("Line1.Speed") == f"ns={device.namespace_index};s=Line1.Speed"
    assert device.node_id("Counter") == f"ns={device.namespace_index};i=1001"
    objects = client.nodes.objects
    line = await objects.get_child([f"{device.namespace_index}:Line1"])
    assert (await line.read_description()).Text == "Main line"
    speed = await line.get_child([f"{device.namespace_index}:Speed"])
    assert await speed.read_value() == 1.5
    assert (await speed.read_data_type_as_variant_type()) == ua.VariantType.Double


async def test_api_changes_reach_clients(device, client):
    device.set("Line1.Speed", 2.25)
    device.set("Line1.Product", "B-7")
    await asyncio.sleep(0)
    assert await node(client, device, "Line1.Speed").read_value() == 2.25
    assert await node(client, device, "Line1.Product").read_value() == "B-7"


async def test_client_writes_are_recorded(device, client):
    await node(client, device, "Line1.Setpoint").write_value(ua.Variant(85, ua.VariantType.Int32))
    event = await device.wait_for_write("Line1.Setpoint", timeout=2)
    assert event.points == ("Line1.Setpoint",)
    assert event.values == (85,)
    assert device.get("Line1.Setpoint") == 85


async def test_rejected_writes_are_not_recorded(device, client):
    with pytest.raises(UaStatusCodeError):  # not writable
        await node(client, device, "Line1.Speed").write_value(
            ua.Variant(9.0, ua.VariantType.Double)
        )
    with pytest.raises(UaStatusCodeError):  # type mismatch
        await node(client, device, "Line1.Setpoint").write_value(
            ua.Variant("x", ua.VariantType.String)
        )
    assert device.writes == []


async def test_generator_and_subscription(device, client):
    received: list[float] = []

    class Handler:
        def datachange_notification(self, _node, value, _data):
            received.append(value)

    subscription = await client.create_subscription(20, Handler())
    await subscription.subscribe_data_change(node(client, device, "Level"))
    await asyncio.sleep(0.4)
    assert len(set(received)) > 2  # the ramp keeps moving
    await subscription.delete()


async def test_virtual_clock_scenario():
    clock = VirtualClock()
    scenario = [
        {"at": 5, "set": {"Line1.Running": False}},
        {"at": 6, "fault": {"type": "status", "code": "BadTimeout"}, "duration": 1},
    ]
    async with OpcUaDevice(spec(scenario=scenario), clock=clock) as device:
        await clock.advance(5)
        assert device.get("Line1.Running") is False
        await clock.advance(1)
        assert [f.kind for f in device.faults.active] == ["status"]
        await clock.advance(1)
        assert device.faults.active == []


# --------------------------------------------------------------------------- faults


async def test_status_fault_is_answered_by_the_proxy(device, client):
    speed = node(client, device, "Line1.Speed")
    with device.fault("status", code="BadTimeout", services=["Read"], count=1) as fault:
        with pytest.raises(BadTimeout):
            await speed.read_value()
        assert fault.hits == 1
    assert await speed.read_value() == 1.5


async def test_numeric_status_code(device, client):
    code = int(ua.StatusCodes.BadServerHalted)
    with (
        device.fault("status", code=code, services=["Read"]),
        pytest.raises(UaStatusCodeError) as info,
    ):
        await node(client, device, "Line1.Speed").read_value()
    assert info.value.code == ua.StatusCodes.BadServerHalted


async def test_service_filter(device, client):
    with device.fault("status", services=["Write"]):
        assert await node(client, device, "Line1.Speed").read_value() == 1.5


async def test_latency(device, client):
    loop = asyncio.get_running_loop()
    with device.fault("latency", delay="250ms", services=["Read"]):
        start = loop.time()
        await node(client, device, "Line1.Speed").read_value()
        assert loop.time() - start >= 0.24


async def test_dropped_response_keeps_the_channel_alive(device, client):
    speed = node(client, device, "Line1.Speed")
    with device.fault("drop", services=["Read"], count=1), pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(speed.read_value(), 0.3)
    # sequence numbers were renumbered: the same session keeps working
    assert await speed.read_value() == 1.5


async def test_drop_executed_write(device, client):
    setpoint = node(client, device, "Line1.Setpoint")
    with (
        device.fault("drop", executed=True, services=["Write"], count=1),
        pytest.raises(asyncio.TimeoutError),
    ):
        await asyncio.wait_for(setpoint.write_value(ua.Variant(5, ua.VariantType.Int32)), 0.3)
    await device.wait_for_write("Line1.Setpoint", timeout=1)
    assert device.get("Line1.Setpoint") == 5


async def test_disconnect(device):
    client = Client(device.endpoint, timeout=1)
    await client.connect()
    try:
        with (
            device.fault("disconnect", services=["Read"], count=1),
            pytest.raises((ConnectionError, asyncio.TimeoutError, UaStatusCodeError)),
        ):
            await client.get_node(device.node_id("Line1.Speed")).read_value()
    finally:
        with contextlib.suppress(Exception):  # the connection is already gone
            await asyncio.wait_for(client.disconnect(), 1)
    async with Client(device.endpoint, timeout=2) as fresh:
        assert await fresh.get_node(device.node_id("Line1.Speed")).read_value() == 1.5


@pytest.mark.parametrize("mode", ["truncate", "garbage", "bad_length", "bit_flip"])
async def test_corrupt_response_breaks_the_read(device, mode):
    client = Client(device.endpoint, timeout=0.5)
    await client.connect()
    try:
        with device.fault("corrupt", mode=mode, services=["Read"], count=1):
            try:
                value = await asyncio.wait_for(
                    client.get_node(device.node_id("Line1.Speed")).read_value(), 1
                )
            except Exception:
                value = None
        # a flipped bit may land somewhere harmless; anything else must fail
        assert value != 1.5 or mode == "bit_flip"
    finally:
        with contextlib.suppress(Exception):  # the channel may be broken on purpose
            await asyncio.wait_for(client.disconnect(), 1)


async def test_fault_validation(device):
    with pytest.raises(ValueError, match="use 'status' with OPC UA"):
        device.fault("exception", code=6)
    with pytest.raises(ValueError, match="unknown OPC UA status code 'BadBanana'"):
        device.fault("status", code="BadBanana")
    with pytest.raises(ValueError, match="does not exist for OPC UA"):
        device.fault("corrupt", mode="bad_crc")


# --------------------------------------------------------------------------- lifecycle


async def test_offline_and_back(device):
    endpoint = device.endpoint
    async with device.offline():
        with pytest.raises(OSError):  # noqa: PT011
            await asyncio.open_connection(device.host, device.port)
        device.set("Line1.Speed", 3.0)
    assert device.endpoint == endpoint
    async with Client(endpoint, timeout=2) as client:
        assert await client.get_node(device.node_id("Line1.Speed")).read_value() == 3.0


async def test_restart_keeps_values():
    device = OpcUaDevice(spec())
    await device.start()
    device.set("Line1.Speed", 4.5)
    await device.stop()
    assert device.server is None
    await device.start()
    try:
        async with Client(device.endpoint, timeout=2) as client:
            assert await client.get_node(device.node_id("Line1.Speed")).read_value() == 4.5
    finally:
        await device.stop()


async def test_port_in_use():
    async with OpcUaDevice(spec()) as first:
        clash = OpcUaDevice(spec(port=first.port))
        with pytest.raises(OSError):  # noqa: PT011
            await clash.start()
        assert clash.server is None


async def test_wildcard_host_endpoint():
    async with OpcUaDevice(spec(host="0.0.0.0")) as device:
        assert device.endpoint.startswith("opc.tcp://127.0.0.1:")
        async with Client(device.endpoint, timeout=2) as client:
            assert await client.get_node(device.node_id("Line1.Speed")).read_value() == 1.5
