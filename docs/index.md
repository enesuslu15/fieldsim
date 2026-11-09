# fieldsim

**Virtual industrial devices for testing software without hardware.**

fieldsim starts fake **Modbus**, **OPC UA** and **Siemens S7** devices from a YAML
file, drives their values with generators, breaks them on purpose and plays
scripted scenarios, so the gateway, SCADA connector or edge application you are
writing can be tested in CI with no PLC on your desk.

```python
import pytest


@pytest.mark.fieldsim("boiler.yaml")
def test_gateway_survives_a_slow_plc(modbus_device):
    modbus_device.set("temperature", 85.0)
    with modbus_device.fault("latency", delay="800ms"):
        assert Gateway(*modbus_device.address, timeout=1).temperature() == 85.0
```

## What you get

**Devices from a file**
:   Register maps, OPC UA node trees and S7 data blocks with typed points,
    byte and word order, initial values and descriptions.
    [Device files →](guide/devices.md)

**Living values**
:   Sine, ramp, random walk and sequence generators, so the data looks like a
    running machine. [Generators →](guide/generators.md)

**Fault injection**
:   Latency, lost responses, disconnects, exception and status codes,
    corrupted frames and full outages, per service or function code.
    [Faults →](guide/faults.md)

**Scenarios and virtual time**
:   "Overheat after 10 minutes, then drop the link for 30 seconds", played
    instantly and identically every run. [Scenarios →](guide/scenarios.md)

**pytest fixtures**
:   `modbus_device`, `opcua_device`, `s7_device` and factories; devices on free
    ports in a background thread, for sync, asyncio and xdist tests.
    [pytest plugin →](guide/pytest.md)

**CI ready**
:   A CLI, a Docker image with a health check and copy-paste GitHub Actions
    jobs. [Docker and CI →](guide/docker-ci.md)


## Protocols

| Protocol | `protocol:` | Built on | Status |
|---|---|---|---|
| Modbus TCP | `modbus_tcp` | fieldsim's own asyncio server | stable |
| Modbus RTU over TCP | `modbus_rtu_over_tcp` | fieldsim's own asyncio server | stable |
| OPC UA (binary, security None) | `opcua` | [asyncua](https://github.com/FreeOpcUa/opcua-asyncio) | stable |
| Siemens S7comm (ISO-on-TCP) | `s7` | [python-snap7](https://github.com/gijzelaerr/python-snap7) | experimental |

Faults for OPC UA and S7 are applied by a small protocol-aware proxy in front of
the server, so every protocol supports the same fault catalogue.
[How it works →](architecture.md)

## Install

```bash
pip install fieldsim             # Modbus
pip install "fieldsim[opcua]"    # + OPC UA
pip install "fieldsim[s7]"       # + S7
pip install "fieldsim[all]"      # everything
```

Then follow [Getting started](getting-started.md).
