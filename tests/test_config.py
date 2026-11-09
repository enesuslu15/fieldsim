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
    msg = errors_of(config(device(protocol="profinet")))
    assert "devices[0]: Input tag 'profinet'" in msg
    assert "'modbus_tcp', 'modbus_rtu_over_tcp', 'opcua'" in msg


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


# --------------------------------------------------------------------------- faults & scenario

POINTS = {
    "coils": [{"name": "run", "address": 0}],
    "holding_registers": [{"name": "sp", "address": 0, "type": "int16"}],
}


def test_faults_and_scenario_parse():
    cfg = parse_config(
        config(
            device(
                registers=POINTS,
                seed=3,
                faults=[{"type": "latency", "delay": "50ms", "probability": 0.5}],
                scenario=[
                    {"at": "1s", "set": {"run": True, "sp": -5}},
                    {"at": "2s", "fault": {"type": "drop", "count": 3}, "duration": "1s"},
                    {"at": "3s", "offline": "2s"},
                    {"at": "4s", "release": ["sp"]},
                ],
            )
        )
    )
    dev = cfg.devices[0]
    assert dev.faults[0].delay == 0.05
    assert [s.at for s in dev.scenario] == [1, 2, 3, 4]


@pytest.mark.parametrize(
    ("step", "message"),
    [
        ({"at": 1}, "exactly one of 'set', 'release', 'fault', 'offline'"),
        ({"at": 1, "set": {"run": 1}, "offline": 1}, "(got set, offline)"),
        ({"at": 1, "set": {"run": 1}, "duration": 1}, "'duration' only applies to 'fault'"),
        ({"at": 1, "offline": 0}, "offline duration must be > 0"),
        ({"at": 1, "set": {"nope": 1}}, "scenario[0]: unknown point 'nope'"),
        ({"at": 1, "release": ["nope"]}, "scenario[0]: unknown point 'nope'"),
        ({"at": 1, "set": {"sp": 99999}}, "scenario[0]: sp: 99999 is out of range for int16"),
        (
            {"at": 1, "fault": {"type": "corrupt", "mode": "bad_crc"}},
            "corrupt mode 'bad_crc' needs RTU framing",
        ),
    ],
)
def test_scenario_validation(step, message):
    assert message in errors_of(config(device(registers=POINTS, scenario=[step])))


@pytest.mark.parametrize(
    ("protocol", "mode", "message"),
    [
        ("modbus_tcp", "bad_crc", "needs RTU framing"),
        ("modbus_rtu_over_tcp", "transaction_id", "only exists in Modbus TCP"),
        ("modbus_rtu_over_tcp", "bad_length", "only exists in Modbus TCP"),
    ],
)
def test_corrupt_mode_must_fit_protocol(protocol, mode, message):
    fault = {"type": "corrupt", "mode": mode}
    assert message in errors_of(config(device(protocol=protocol, faults=[fault])))


def test_fault_errors_have_clean_locations():
    msg = errors_of(config(device(faults=[{"type": "latency"}])))
    assert "devices[0].faults[0].delay: Field required" in msg


# --------------------------------------------------------------------------- OPC UA

OPCUA_NODES = [
    {
        "name": "Line1",
        "nodes": [
            {"name": "Speed", "type": "Double", "value": 1.5},
            {"name": "Name", "type": "String", "value": "a"},
        ],
    },
    {"name": "Count", "type": "UInt16", "node_id": "i=7"},
]


def opcua(**overrides):
    data = {"name": "line", "protocol": "opcua", "nodes": OPCUA_NODES}
    data.update(overrides)
    return data


def test_opcua_device_parses():
    dev = parse_config(config(opcua())).devices[0]
    assert dev.port == 4840
    assert dev.point_names() == ["Line1.Speed", "Line1.Name", "Count"]
    assert dev.namespace_uri == "urn:fieldsim:line"
    assert dev.endpoint() == "opc.tcp://127.0.0.1:4840/"
    assert dev.endpoint(host="h", port=1) == "opc.tcp://h:1/"
    assert parse_config(config(opcua(namespace="urn:x"))).devices[0].namespace_uri == "urn:x"


def test_mixed_protocols_in_one_file():
    cfg = parse_config(config(device(), opcua(port=4841)))
    assert [d.protocol for d in cfg.devices] == ["modbus_tcp", "opcua"]


@pytest.mark.parametrize(
    ("nodes", "message"),
    [
        ([{"name": "A"}, {"name": "A"}], "duplicate node name 'A'"),
        (
            [{"name": "F", "nodes": [{"name": "x"}, {"name": "x"}]}],
            "nodes.F: duplicate node name 'x'",
        ),
        ([{"name": "a", "node_id": "i=1"}, {"name": "b", "node_id": "i=1"}], "share node id 'i=1'"),
        ([{"name": "a.b"}], "nodes[0].name"),
        ([{"name": "a", "type": "Int16", "value": 1e6}], "out of range for Int16"),
        ([{"name": "a", "type": "String", "value": 3}], "expected a string"),
        ([{"name": "a", "node_id": "x=1"}], "node_id"),
        (
            [{"name": "a", "type": "String", "generator": {"type": "constant", "value": 1}}],
            "String variables cannot have a generator",
        ),
        ([{"name": "a", "type": "Decimal"}], "nodes[0].type"),
    ],
)
def test_opcua_node_validation(nodes, message):
    assert message in errors_of(config(opcua(nodes=nodes)))


def test_opcua_scenario_values_are_type_checked():
    ok = [{"at": 1, "set": {"Line1.Name": "b", "Line1.Speed": 2}}]
    parse_config(config(opcua(scenario=ok)))
    bad = [{"at": 1, "set": {"Line1.Name": 5}}]
    assert "scenario[0]: Line1.Name: expected a string" in errors_of(config(opcua(scenario=bad)))


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ({"type": "exception"}, "use 'status' with OPC UA"),
        ({"type": "drop", "function_codes": [3]}, "use 'services' with OPC UA"),
        ({"type": "corrupt", "mode": "transaction_id"}, "does not exist for OPC UA"),
        ({"type": "status", "code": "Good"}, "start with 'Bad' or 'Uncertain'"),
        ({"type": "status", "code": 0}, "32-bit unsigned"),
        ({"type": "drop", "services": ["Teleport"]}, "faults[0].services[0]"),
    ],
)
def test_opcua_fault_validation(fault, message):
    assert message in errors_of(config(opcua(faults=[fault])))


def test_opcua_faults_parse():
    faults = [
        {"type": "status", "code": "BadTimeout", "services": ["Read", "Write"]},
        {"type": "status", "code": 2148139008},
        {"type": "corrupt", "mode": "bad_length"},
    ]
    assert len(parse_config(config(opcua(faults=faults))).devices[0].faults) == 3


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ({"type": "status"}, "use 'exception' with Modbus"),
        ({"type": "drop", "services": ["Read"]}, "use 'function_codes' with Modbus"),
    ],
)
def test_modbus_rejects_opcua_faults(fault, message):
    assert message in errors_of(config(device(faults=[fault])))


def test_modbus_scenario_rejects_strings():
    regs = {"coils": [{"name": "run", "address": 0}]}
    step = {"at": 1, "set": {"run": "on"}}
    msg = errors_of(config(device(registers=regs, scenario=[step])))
    assert "scenario[0]: run: expected a boolean, got a string" in msg


# --------------------------------------------------------------------------- S7


def s7(**overrides):
    data = {
        "name": "press",
        "protocol": "s7",
        "data_blocks": [{"number": 1, "points": [{"name": "p", "offset": 0, "type": "REAL"}]}],
    }
    data.update(overrides)
    return data


def test_s7_defaults_and_areas():
    dev = parse_config(config(s7(merkers={"size": 8}))).devices[0]
    assert (dev.port, dev.rack, dev.slot) == (102, 0, 1)
    assert [(code, number) for code, number, _ in dev.areas()] == [("DB", 1), ("MK", 0)]
    assert dev.point_names() == ["p"]
    assert dev.areas()[0][2].byte_size == 4


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"data_blocks": [{"number": 1}, {"number": 1}]}, "duplicate data block number(s): [1]"),
        (
            {"merkers": {"points": [{"name": "p", "offset": 0, "type": "BYTE"}]}},
            "duplicate point name 'p'",
        ),
        (
            {
                "data_blocks": [
                    {"number": 1, "size": 2, "points": [{"name": "x", "offset": 0, "type": "REAL"}]}
                ]
            },
            "ends after the area size of 2 bytes",
        ),
        ({"data_blocks": [{"number": 0}]}, "data_blocks[0].number"),
        ({"rack": 9}, "rack"),
        ({"faults": [{"type": "exception"}]}, "not supported for S7 devices yet"),
        ({"faults": [{"type": "drop", "function_codes": [3]}]}, "use 'services' with S7"),
        ({"scenario": [{"at": 1, "set": {"p": "hot"}}]}, "scenario[0]: p: expected a number"),
    ],
)
def test_s7_validation(overrides, message):
    assert message in errors_of(config(s7(**overrides)))
