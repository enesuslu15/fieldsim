# Getting started

This page takes you from nothing to a pytest test that talks to a simulated PLC.

## 1. Install

```bash
pip install fieldsim pymodbus
```

`pymodbus` is only needed for this walkthrough, as a stand-in for the Modbus
client in your own application.

## 2. Describe a device

```bash
fieldsim init boiler.yaml
```

writes a starter file. Trim it to this:

```yaml title="boiler.yaml"
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
```

Check it:

```console
$ fieldsim validate boiler.yaml
OK    boiler.yaml: 1 device, 2 points
```

Mistakes are reported with their exact location, for example
`devices[0].registers.holding_registers[1]: 99999 is out of range for uint16 [0, 65535]`.

## 3. Run it

```console
$ fieldsim run boiler.yaml
boiler-plc           modbus_tcp           127.0.0.1:5020  (2 points, 1 generated)
serving - press Ctrl+C to stop
```

Point any Modbus client (or your application) at `127.0.0.1:5020`: holding
registers 0–1 contain a float that swings between 60 and 90.

## 4. Write a test

The pytest plugin is installed with fieldsim. Put the YAML file next to your
tests and use the `modbus_device` fixture:

```python title="test_boiler.py"
import pytest
from pymodbus.client import ModbusTcpClient

pytestmark = pytest.mark.fieldsim("boiler.yaml")


def read_temperature(host, port):
    with ModbusTcpClient(host, port=port) as client:
        regs = client.read_holding_registers(0, count=2).registers
        return client.convert_from_registers(regs, client.DATATYPE.FLOAT32)


def test_reads_the_temperature(modbus_device):
    modbus_device.set("temperature", 85.5)  # pauses the generator
    assert read_temperature(*modbus_device.address) == 85.5


def test_reports_a_busy_device(modbus_device):
    with modbus_device.fault("exception", code=6):  # SERVER DEVICE BUSY
        with pytest.raises(Exception):
            read_temperature(*modbus_device.address)
```

```console
$ pytest -q
..                                                         [100%]
2 passed in 0.31s
```

Every test gets its own device on a free port, so tests never interfere with
each other and can run in parallel with `pytest -n auto`.

## Next steps

- Model your real device: [Device files](guide/devices.md)
- Break it in interesting ways: [Faults and outages](guide/faults.md)
- Script long-running behaviour: [Scenarios and virtual time](guide/scenarios.md)
- All fixtures and options: [pytest plugin](guide/pytest.md)
