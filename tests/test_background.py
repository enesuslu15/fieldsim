import asyncio
import socket
import struct
import threading

import pytest

from fieldsim import BackgroundSimulator, SimulationConfig
from fieldsim.background import resolve_config, with_free_ports

from .conftest import modbus_spec

READ_HR0 = b"\x00\x01\x00\x00\x00\x06\x01\x03\x00\x00\x00\x01"
WRITE_HR0 = b"\x00\x02\x00\x00\x00\x06\x01\x06\x00\x00"


def config(**overrides) -> SimulationConfig:
    return SimulationConfig(devices=[modbus_spec(**overrides)])


def read_hr0(handle) -> int:
    with socket.create_connection(handle.address, timeout=2) as s:
        s.sendall(READ_HR0)
        return struct.unpack(">H", s.recv(64)[-2:])[0]


def write_hr0(handle, value: int) -> None:
    with socket.create_connection(handle.address, timeout=2) as s:
        s.sendall(WRITE_HR0 + struct.pack(">H", value))
        s.recv(64)


@pytest.fixture
def sim():
    with BackgroundSimulator(config()) as s:
        yield s


def test_resolve_config(tmp_path):
    cfg = config()
    assert resolve_config(cfg) is cfg
    assert (
        resolve_config({"devices": [{"name": "a", "protocol": "modbus_tcp"}]}).devices[0].name
        == "a"
    )
    path = tmp_path / "s.yaml"
    path.write_text("devices: [{name: b, protocol: modbus_tcp}]", encoding="utf-8")
    assert resolve_config(path).devices[0].name == "b"
    assert resolve_config(str(path)).devices[0].name == "b"


def test_with_free_ports():
    cfg = SimulationConfig(devices=[modbus_spec(port=5020)])
    assert with_free_ports(cfg).devices[0].port == 0
    assert cfg.devices[0].port == 5020


def test_sync_access(sim):
    plc = sim["plc"]
    assert plc.port > 0
    assert plc.protocol == "modbus_tcp"
    assert plc.unit_id == 1
    assert read_hr0(plc) == 75
    plc.set("setpoint", 99)
    assert read_hr0(plc) == 99
    assert plc.get("setpoint") == 99
    plc.release("setpoint")
    assert "plc" in repr(plc)
    assert list(sim) == [plc]
    assert set(sim.devices) == {"plc"}


def test_errors_are_raised_in_the_caller(sim):
    with pytest.raises(KeyError, match="no point named"):
        sim["plc"].get("nope")
    with pytest.raises(KeyError, match="available: plc"):
        sim["other"]


def test_faults_and_hits(sim):
    plc = sim["plc"]
    with plc.fault("drop", count=1) as fault:
        assert fault.kind == "drop"
        assert fault.active
        with socket.create_connection(plc.address, timeout=0.2) as s:
            s.sendall(READ_HR0)
            with pytest.raises(TimeoutError):
                s.recv(64)
        assert fault.hits == 1
        assert not fault.active
        assert "drop" in repr(fault)
    plc.fault("exception", code=6)
    plc.clear_faults()
    assert read_hr0(plc) == 75


def test_offline_context(sim):
    plc = sim["plc"]
    with plc.offline():
        assert not plc.online
        with pytest.raises(OSError):  # noqa: PT011
            socket.create_connection(plc.address, timeout=0.5)
    assert plc.online
    assert read_hr0(plc) == 75


def test_wait_for_write_sync(sim):
    plc = sim["plc"]
    threading.Timer(0.05, write_hr0, args=(plc, 321)).start()
    event = plc.wait_for_write("setpoint", timeout=2)
    assert event.values == (321,)
    assert len(plc.writes) == 1
    plc.clear_writes()
    assert plc.writes == []


async def test_async_variants_do_not_block_the_test_loop(sim):
    plc = sim["plc"]

    async def client():
        await asyncio.sleep(0.05)
        reader, writer = await asyncio.open_connection(*plc.address)
        writer.write(WRITE_HR0 + b"\x00\x07")
        await reader.read(12)
        writer.close()

    task = asyncio.create_task(client())
    event = await plc.wait_for_write_async("setpoint", timeout=2)
    await task
    assert event.values == (7,)


def test_virtual_clock_and_scenario():
    cfg = config(scenario=[{"at": "1h", "set": {"setpoint": 500}}])
    with BackgroundSimulator(cfg, clock="virtual") as sim:
        plc = sim["plc"]
        assert sim.time() == 0
        assert not plc.scenario_done
        plc.advance(3599)
        assert plc.get("setpoint") == 75
        plc.advance(1)
        assert plc.get("setpoint") == 500
        assert plc.scenario_done
        assert sim.time() == 3600


async def test_advance_async():
    cfg = config(scenario=[{"at": 5, "set": {"setpoint": 1}}])
    with BackgroundSimulator(cfg, clock="virtual") as sim:
        await sim["plc"].advance_async(5)
        assert sim["plc"].get("setpoint") == 1


def test_advance_needs_virtual_clock(sim):
    with pytest.raises(RuntimeError, match="virtual clock"):
        sim.advance(1)


def test_invalid_clock_kind():
    with pytest.raises(ValueError, match="clock must be"):
        BackgroundSimulator(config(), clock="sundial")  # type: ignore[arg-type]


def test_startup_error_is_raised_and_simulator_can_be_reused():
    with BackgroundSimulator(config()) as blocker:
        sim = BackgroundSimulator(config(port=blocker["plc"].port))
        with pytest.raises(OSError):  # noqa: PT011
            sim.start()
        assert not sim.running
    sim.start()  # the port is free now
    sim.stop()


def test_lifecycle_guards():
    sim = BackgroundSimulator(config())
    with pytest.raises(RuntimeError, match="not running"):
        sim.call(lambda: None)
    sim.stop()  # stopping a never-started simulator is fine
    sim.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            sim.start()

        def from_inside():
            return sim.call(lambda: None)

        with pytest.raises(RuntimeError, match="deadlock"):
            sim.call(lambda: from_inside())
    finally:
        sim.stop()
    assert not sim.running
    with pytest.raises(KeyError, match="not running"):
        sim["plc"]


def test_fault_remove_after_stop_is_harmless():
    with BackgroundSimulator(config()) as sim:
        fault = sim["plc"].fault("drop")
    fault.remove()


def test_opcua_only_attributes_on_modbus(sim):
    with pytest.raises(AttributeError, match="no OPC UA endpoint"):
        _ = sim["plc"].endpoint
    with pytest.raises(AttributeError, match="no OPC UA node ids"):
        sim["plc"].node_id("setpoint")


def test_opcua_handle():
    cfg = {"devices": [{"name": "l", "protocol": "opcua", "port": 0, "nodes": [{"name": "S"}]}]}
    with BackgroundSimulator(cfg) as s:
        handle = s["l"]
        assert handle.endpoint.startswith("opc.tcp://127.0.0.1:")
        assert handle.node_id("S").endswith(";s=S")
