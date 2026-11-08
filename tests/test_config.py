import textwrap

import pytest

from fieldsim.config import (
    ConfigError,
    SimulationConfig,
    load_config,
    parse_config,
    parse_duration,
)


def device(**overrides):
    data = {"name": "plc", "protocol": "modbus_tcp", "port": 5020}
    data.update(overrides)
    return data


def config(*devices):
    return {"version": 1, "devices": list(devices) or [device()]}


def errors_of(data) -> str:
    with pytest.raises(ConfigError) as info:
        parse_config(data)
    return str(info.value)


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [(2, 2.0), (0.5, 0.5), ("500ms", 0.5), ("2s", 2.0), ("1.5m", 90.0), ("1h", 3600.0), ("3", 3.0)],
)
def test_parse_duration(raw, seconds):
    assert parse_duration(raw) == seconds


@pytest.mark.parametrize("raw", ["fast", "1d", "-1s", -1, True, None, [1]])
def test_parse_duration_invalid(raw):
    with pytest.raises(ValueError):  # noqa: PT011
        parse_duration(raw)


def test_minimal_config_defaults():
    cfg = parse_config(config())
    dev = cfg.devices[0]
    assert dev.host == "127.0.0.1"
    assert dev.unit_id is None
    assert dev.unmapped == "error"
    assert dev.update_interval == pytest.approx(0.1)
    assert list(dev.registers.all_points()) == []


def test_full_example_loads(tmp_path):
    from pathlib import Path

    example = Path(__file__).parent.parent / "examples" / "boiler.yaml"
    cfg = load_config(example)
    assert cfg.devices[0].name == "boiler-plc"
    assert len(list(cfg.devices[0].registers.all_points())) == 7


def test_unknown_key_is_rejected():
    msg = errors_of(config(device(prot=1)))
    assert "devices[0].prot: Extra inputs are not permitted" in msg


def test_unknown_protocol():
    assert "devices[0].protocol" in errors_of(config(device(protocol="profinet")))


def test_devices_required():
    assert "devices" in errors_of({"version": 1, "devices": []})


def test_top_level_must_be_mapping():
    assert "top level must be a mapping" in errors_of([1, 2])


def test_duplicate_device_names():
    assert "duplicate device name 'plc'" in errors_of(config(device(), device(port=5021)))


def test_port_clash():
    msg = errors_of(config(device(name="a"), device(name="b")))
    assert "both listen on 127.0.0.1:5020" in msg


def test_port_zero_never_clashes():
    parse_config(config(device(name="a", port=0), device(name="b", port=0)))


def test_overlapping_registers():
    regs = {
        "holding_registers": [
            {"name": "a", "address": 0, "type": "float32"},
            {"name": "b", "address": 1},
        ]
    }
    msg = errors_of(config(device(registers=regs)))
    assert "'b' overlaps 'a' at address 1" in msg


def test_same_address_in_different_tables_is_fine():
    regs = {
        "coils": [{"name": "a", "address": 0}],
        "holding_registers": [{"name": "b", "address": 0}],
    }
    parse_config(config(device(registers=regs)))


def test_duplicate_point_names_across_tables():
    regs = {
        "coils": [{"name": "x", "address": 0}],
        "input_registers": [{"name": "x", "address": 0}],
    }
    assert "duplicate point name 'x'" in errors_of(config(device(registers=regs)))


def test_value_out_of_range_for_type():
    regs = {"holding_registers": [{"name": "a", "address": 0, "type": "int16", "value": 40000}]}
    msg = errors_of(config(device(registers=regs)))
    assert "holding_registers[0]: 40000 is out of range for int16" in msg


def test_register_past_end_of_address_space():
    regs = {"holding_registers": [{"name": "a", "address": 65535, "type": "float32"}]}
    assert "runs past address 65535" in errors_of(config(device(registers=regs)))


def test_writable_not_allowed_on_input_registers():
    regs = {"input_registers": [{"name": "a", "address": 0, "writable": True}]}
    assert "writable" in errors_of(config(device(registers=regs)))


def test_invalid_point_name():
    regs = {"coils": [{"name": "has space", "address": 0}]}
    assert "coils[0].name" in errors_of(config(device(registers=regs)))


@pytest.mark.parametrize(
    ("generator", "message"),
    [
        ({"type": "sine", "min": 10, "max": 0, "period": 1}, "min must be <= max"),
        ({"type": "sine", "min": 0, "max": 1, "period": 0}, "period must be > 0"),
        ({"type": "ramp", "start": 0, "end": 1, "period": "0s"}, "period must be > 0"),
        (
            {"type": "random_walk", "start": 5, "step": 1, "min": 3, "max": 1},
            "min must be <= max",
        ),
        (
            {"type": "random_walk", "start": 9, "step": 1, "min": 0, "max": 5},
            "start must be between min and max",
        ),
        ({"type": "sequence", "values": [1], "interval": 0}, "interval must be > 0"),
        ({"type": "sequence", "values": [], "interval": 1}, "at least 1 item"),
        ({"type": "square"}, "does not match any of the expected tags"),
    ],
)
def test_generator_validation(generator, message):
    regs = {"holding_registers": [{"name": "a", "address": 0, "generator": generator}]}
    msg = errors_of(config(device(registers=regs)))
    assert message in msg
    assert "holding_registers[0].generator" in msg


def test_update_interval_must_be_positive():
    assert "update_interval must be > 0" in errors_of(config(device(update_interval=0)))


def test_load_config_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="cannot read file"):
        load_config(tmp_path / "nope.yaml")


def test_load_config_bad_yaml(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("devices: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(path)


def test_load_config_reports_source_and_errors(tmp_path):
    path = tmp_path / "dev.yaml"
    path.write_text(
        textwrap.dedent(
            """
            devices:
              - name: plc
                protocol: modbus_tcp
                port: 99999
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as info:
        load_config(path)
    assert info.value.source == str(path)
    assert info.value.errors == ["devices[0].port: Input should be less than or equal to 65535"]


def test_json_schema_is_generated():
    schema = SimulationConfig.model_json_schema()
    assert "devices" in schema["properties"]
