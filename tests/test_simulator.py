import pytest

import fieldsim
from fieldsim import Simulator


def two_devices(second_port=0):
    return {
        "devices": [
            {
                "name": "a",
                "protocol": "modbus_tcp",
                "port": 0,
                "registers": {"holding_registers": [{"name": "x", "address": 0, "value": 1}]},
            },
            {"name": "b", "protocol": "modbus_tcp", "port": second_port},
        ]
    }


async def test_start_stop_and_lookup():
    sim = Simulator.from_dict(two_devices())
    assert len(sim) == 2
    async with sim:
        assert all(d.running for d in sim)
        assert sim["a"].get("x") == 1
        assert sim["a"].port != sim["b"].port
    assert not any(d.running for d in sim)


def test_unknown_device_name():
    sim = Simulator.from_dict(two_devices())
    with pytest.raises(KeyError, match="available: a, b"):
        sim["c"]


async def test_failed_start_rolls_back():
    async with Simulator.from_dict(two_devices()) as blocker:
        port = blocker["a"].port
        sim = Simulator.from_dict(two_devices(second_port=port))
        with pytest.raises(OSError):  # noqa: PT011
            await sim.start()
        assert not sim["a"].running


def test_from_yaml(tmp_path):
    path = tmp_path / "sim.yaml"
    path.write_text("devices:\n  - {name: a, protocol: modbus_tcp, port: 0}\n", encoding="utf-8")
    assert Simulator.from_yaml(path)["a"].protocol == "modbus_tcp"


def test_public_api():
    assert fieldsim.__version__
    assert set(fieldsim.__all__) >= {"Simulator", "load_config", "ConfigError"}
