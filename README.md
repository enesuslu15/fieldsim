# fieldsim

[![PyPI](https://img.shields.io/pypi/v/fieldsim)](https://pypi.org/project/fieldsim/)
[![Python](https://img.shields.io/pypi/pyversions/fieldsim)](https://pypi.org/project/fieldsim/)
[![CI](https://github.com/enesuslu15/fieldsim/actions/workflows/ci.yml/badge.svg)](https://github.com/enesuslu15/fieldsim/actions/workflows/ci.yml)
[![Docs](https://github.com/enesuslu15/fieldsim/actions/workflows/docs.yml/badge.svg)](https://enesuslu15.github.io/fieldsim/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

**Virtual industrial devices for testing software without hardware.**

fieldsim starts fake **Modbus**, **OPC UA** and **Siemens S7** devices from a YAML
file, drives their values with generators, breaks them on purpose with fault
injection and plays scripted scenarios — so you can test gateways, SCADA
connectors and edge apps in CI, no PLC on your desk required.

📖 **Documentation: <https://enesuslu15.github.io/fieldsim/>**

```python
import pytest


@pytest.mark.fieldsim("boiler.yaml")
def test_gateway_survives_a_slow_plc(modbus_device):
    modbus_device.set("temperature", 85.0)  # pauses the generator
    with modbus_device.fault("latency", delay="800ms"):
        assert Gateway(*modbus_device.address, timeout=1).temperature() == 85.0


@pytest.mark.fieldsim("boiler.yaml", clock="virtual")
def test_overheat_alarm(modbus_device):
    modbus_device.advance(600)  # ten minutes, instantly
    assert Gateway(*modbus_device.address).alarm_raised()
```

## Install

```bash
pip install fieldsim             # Modbus TCP / RTU over TCP
pip install "fieldsim[opcua]"    # + OPC UA
pip install "fieldsim[s7]"       # + Siemens S7 (experimental)
pip install "fieldsim[all]"      # everything
```

or run it as a container:

```bash
docker run --rm -p 5020:5020 -v "$PWD/boiler.yaml:/config/devices.yaml:ro" ghcr.io/enesuslu15/fieldsim
```

## Features

- **Three protocols** — Modbus TCP and RTU over TCP (own asyncio server), OPC UA
  (on asyncua) and S7comm (on python-snap7, experimental), mixed freely in one file.
- **Devices from YAML** — register maps, OPC UA node trees, S7 data blocks; typed
  points with byte/word order, validated with precise error locations and a JSON
  Schema for editor autocompletion.
- **Generators** — sine, ramp, random walk, sequence, so data looks like a
  running machine.
- **Fault injection** — latency, lost responses (optionally after executing the
  request), disconnects, Modbus exceptions, OPC UA status codes, six kinds of
  corrupted frames, outages; filtered by function code or service, by
  probability or count, reproducible with a seed.
- **Scenarios and virtual time** — "overheat at 30 s, outage at 60 s", played
  instantly and identically on every run.
- **pytest plugin** — `modbus_device`, `opcua_device`, `s7_device`, factories and
  a marker; free ports, background thread, works for sync, asyncio and xdist.
- **Write log** — `wait_for_write("setpoint")` to assert what your code sent.
- **CI ready** — CLI (`init`, `validate`, `run`, `check`, `schema`), a multi-arch
  Docker image with a health check, copy-paste GitHub Actions jobs.

## A device file

```yaml
version: 1
devices:
  - name: boiler-plc
    protocol: modbus_tcp
    port: 5020
    registers:
      holding_registers:
        - name: temperature
          address: 0
          type: float32
          generator: { type: sine, min: 60, max: 90, period: 30s }
        - { name: setpoint, address: 2, type: uint16, value: 75 }
    faults:
      - { type: latency, delay: 20ms, jitter: 10ms }
    scenario:
      - { at: 30s, set: { temperature: 120 } }
      - { at: 40s, fault: { type: exception, code: 6 }, duration: 5s }
      - { at: 60s, offline: 10s }

  - name: packaging-line
    protocol: opcua
    port: 4840
    nodes:
      - name: Line1
        nodes:
          - { name: Speed, type: Double, generator: { type: ramp, start: 0, end: 2, period: 1m } }
          - { name: Product, type: String, value: A-100, writable: true }

  - name: press-plc
    protocol: s7
    port: 1102
    data_blocks:
      - number: 10
        points:
          - { name: pressure, offset: 0, type: REAL, value: 200.0 }    # DB10.DBD0
          - { name: running, offset: 8, bit: 0, type: BOOL }           # DB10.DBX8.0
```

```bash
fieldsim validate plant.yaml
fieldsim run plant.yaml
```

More in the [examples](examples/) folder and the
[documentation](https://enesuslu15.github.io/fieldsim/).

## Contributing

Issues and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).
fieldsim follows [Semantic Versioning](https://semver.org/); changes are listed in
the [changelog](CHANGELOG.md).

## License

MIT
