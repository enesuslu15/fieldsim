# fieldsim

**Virtual industrial devices for testing software without hardware.**

fieldsim starts fake Modbus and OPC UA devices (S7 is on the roadmap) from a YAML
file, drives their values with generators, breaks them on purpose with fault
injection and plays scripted scenarios — so you can test gateways, SCADA
connectors and edge apps in CI, no PLC on your desk required.

> Status: alpha (`0.3.0`). The file format may still change before 1.0.

## Install

```bash
pip install fieldsim             # Modbus TCP / RTU over TCP
pip install "fieldsim[opcua]"    # + OPC UA (uses asyncua)
```

or run it as a container:

```bash
docker run --rm -p 5020:5020 -v "$PWD/boiler.yaml:/config/devices.yaml:ro" ghcr.io/enesuslu15/fieldsim
```

## Test with pytest

Installing fieldsim registers a pytest plugin. With no configuration at all you
get a Modbus TCP device whose whole address space is plain memory:

```python
def test_gateway_writes_registers(modbus_device):
    Gateway(modbus_device.host, modbus_device.port).write_register(100, 42)
    event = modbus_device.wait_for_write(timeout=2)
    assert event.address == 100 and event.values == (42,)
```

Point it at a device file to get named points, generators and scenarios:

```python
import pytest


@pytest.mark.fieldsim("boiler.yaml")
def test_gateway_reads_temperature(modbus_device):
    modbus_device.set("temperature", 85.0)  # pauses the generator
    assert Gateway(*modbus_device.address).temperature() == 85.0


@pytest.mark.fieldsim("boiler.yaml")
def test_gateway_survives_a_slow_plc(modbus_device):
    with modbus_device.fault("latency", delay="800ms"):
        assert Gateway(*modbus_device.address, timeout=1).temperature() is not None


@pytest.mark.fieldsim("boiler.yaml")
def test_gateway_retries_lost_responses(modbus_device):
    with modbus_device.fault("drop", count=2) as fault:
        Gateway(*modbus_device.address, retries=3).temperature()
    assert fault.hits == 2


@pytest.mark.fieldsim("boiler.yaml")
def test_gateway_reconnects_after_outage(modbus_device):
    gw = Gateway(*modbus_device.address)
    with modbus_device.offline():
        assert not gw.healthy()
    assert gw.healthy()


@pytest.mark.fieldsim("boiler.yaml")
def test_gateway_writes_setpoint(modbus_device):
    Gateway(*modbus_device.address).set_setpoint(80)
    modbus_device.wait_for_write("setpoint", timeout=2)
    assert modbus_device.get("setpoint") == 80


@pytest.mark.fieldsim("boiler.yaml", clock="virtual")
def test_overheat_alarm(modbus_device):
    modbus_device.advance(600)  # ten minutes, instantly
    ...
```

OPC UA works the same way:

```python
@pytest.mark.fieldsim("packaging_line.yaml")
async def test_reads_speed(opcua_device):
    opcua_device.set("Line1.Speed", 1.05)
    async with Client(opcua_device.endpoint) as client:
        assert await client.get_node(opcua_device.node_id("Line1.Speed")).read_value() == 1.05


@pytest.mark.fieldsim("packaging_line.yaml")
async def test_reports_timeouts(opcua_device):
    with opcua_device.fault("status", code="BadTimeout", services=["Read"]):
        ...
```

Fixtures and options:

| | |
|---|---|
| `modbus_device`, `opcua_device` | The Modbus / OPC UA device of the simulation (choose with `mark.fieldsim(device="name")` if there are several). |
| `fieldsim` | The whole running simulation: `fieldsim["plc-1"]`, `fieldsim.advance(10)`. |
| `make_modbus_device`, `make_opcua_device` | Factories for ad-hoc devices: `make_modbus_device(registers={...}, unit_id=3)`. |
| `@pytest.mark.fieldsim(config, device=, clock=, free_ports=)` | Per-test configuration. `config` is a YAML path (relative to the test file or rootdir) or a dict. |
| ini `fieldsim_config`, `fieldsim_clock`, `fieldsim_free_ports` | Project-wide defaults. |

Devices run in a background thread on free ports, so the fixtures work in plain,
asyncio and trio tests, and with `pytest-xdist`. In async tests use
`await device.wait_for_write_async(...)` and `await device.advance_async(...)`.

## Describe a device

```yaml
# boiler.yaml
version: 1
devices:
  - name: boiler-plc
    protocol: modbus_tcp          # or modbus_rtu_over_tcp
    port: 5020
    registers:
      coils:
        - { name: pump_on, address: 0, value: true }
      holding_registers:
        - name: temperature
          address: 0
          type: float32
          generator: { type: sine, min: 60, max: 90, period: 30s }
        - { name: setpoint, address: 2, type: uint16, value: 75 }

    faults:                       # active from the start
      - { type: latency, delay: 20ms, jitter: 10ms }

    scenario:                     # relative to device start
      - { at: 30s, set: { temperature: 120 } }
      - { at: 40s, fault: { type: exception, code: 6 }, duration: 5s }
      - { at: 60s, offline: 10s }
      - { at: 90s, release: [temperature] }
```

An OPC UA device is a tree of folders and variables; a variable's point name is
its dotted path and its node id `ns=<index>;s=<path>` unless you set `node_id`:

```yaml
  - name: packaging-line
    protocol: opcua
    port: 4840                    # endpoint opc.tcp://127.0.0.1:4840/
    nodes:
      - name: Line1
        nodes:
          - { name: Speed, type: Double, generator: { type: sine, min: 0.8, max: 1.2, period: 20s } }
          - { name: Setpoint, type: Int32, value: 120, writable: true }
          - { name: Product, type: String, value: A-100, writable: true, node_id: "i=1001" }
    scenario:
      - { at: 40s, fault: { type: status, code: BadTimeout, services: [Read] }, duration: 5s }
```

Variable types: `Boolean SByte Byte Int16 UInt16 Int32 UInt32 Int64 UInt64 Float Double String`.
Values set from code or written by clients are pushed to subscriptions.

## Command line

```bash
fieldsim init plant.yaml --protocol opcua   # starter file (modbus or opcua)
fieldsim validate plant.yaml                # check without starting
fieldsim run plant.yaml --host 0.0.0.0      # serve until Ctrl+C (--duration N to stop)
fieldsim check plant.yaml --wait 30         # exit 0 once every device accepts connections
fieldsim schema -o fieldsim.schema.json     # JSON Schema for editor autocompletion
```

## Docker and CI

The image `ghcr.io/enesuslu15/fieldsim` (amd64, arm64) runs
`fieldsim run /config/devices.yaml --host 0.0.0.0` and has a health check, so
other services can wait for it with `depends_on: {condition: service_healthy}`.
See [`examples/docker-compose.yml`](examples/docker-compose.yml) and, for GitHub
Actions with either the pytest plugin or the container,
[`examples/github-actions.yml`](examples/github-actions.yml).

### Device options

| Key | Meaning |
|---|---|
| `protocol` | `modbus_tcp`, `modbus_rtu_over_tcp` (RTU frames with CRC tunnelled over TCP) or `opcua`. |
| `host`, `port` | Listen address. `port: 0` picks a free port. |
| `unit_id` | Modbus: only answer this unit id (default: answer all). |
| `unmapped` | Modbus: `error` (exception 02 for undefined addresses, default) or `allow` (plain memory). |
| `path`, `namespace` | OPC UA: endpoint URL path (default `/`) and namespace URI. |
| `update_interval` | Generator refresh rate, e.g. `100ms`. |
| `faults` | Faults active from the start (see below). |
| `scenario` | Timed steps: `set`, `release`, `fault` (+ `duration`), `offline`. |
| `seed` | Makes random faults reproducible. |

Points live in `registers.coils`, `discrete_inputs`, `holding_registers` and
`input_registers`, with `name`, `address`, `value`, `generator`, `description` and —
for registers — `type` (`int16 uint16 int32 uint32 int64 uint64 float32 float64`),
`byte_order` and `word_order`. Coils and holding registers take `writable: false`.

Generators: `constant`, `sine`, `ramp` (`sawtooth` / `triangle` / `once`),
`random_walk`, `sequence`.

### Faults

| Type | Effect |
|---|---|
| `latency` | Delay responses by `delay` + random `0..jitter`. |
| `drop` | No response. `executed: true` still performs the request (lost reply). |
| `disconnect` | Close the connection instead of answering. |
| `exception` | Modbus: answer with exception `code` (default 6, SERVER DEVICE BUSY). |
| `status` | OPC UA: answer with a ServiceFault carrying `code` (e.g. `BadTimeout`). |
| `corrupt` | Damage the response: `bit_flip`, `truncate`, `garbage`, `bad_length` (Modbus TCP, OPC UA), `transaction_id` (Modbus TCP), `bad_crc` (Modbus RTU). |

Every fault accepts `probability` (0..1), `count` (remove after N hits) and a
filter: `function_codes` for Modbus, `services` (`Read`, `Write`, `Browse`,
`Publish`, ...) for OPC UA.

OPC UA faults are applied by a small proxy in front of the server that matches
responses to requests and renumbers the secure channel's sequence numbers, so a
dropped or injected message never breaks the session by itself.

## Use from Python

```python
from fieldsim import Simulator, VirtualClock

clock = VirtualClock()
async with Simulator.from_yaml("boiler.yaml", clock=clock) as sim:
    plc = sim["boiler-plc"]
    with plc.fault("corrupt", mode="bit_flip", probability=0.1):
        await clock.advance(60)
```

From synchronous code use `BackgroundSimulator("boiler.yaml")` as a context
manager; its device handles are thread-safe.

## Supported Modbus function codes

01 Read Coils · 02 Read Discrete Inputs · 03 Read Holding Registers · 04 Read Input Registers ·
05 Write Single Coil · 06 Write Single Register · 15 Write Multiple Coils · 16 Write Multiple Registers

## Development

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows; use .venv/bin/activate elsewhere
pip install -e ".[dev]"
pre-commit install
coverage run -m pytest && coverage report
```

`coverage run` is used instead of `pytest --cov` because fieldsim's own pytest
plugin is imported before pytest-cov starts measuring.

## License

MIT
