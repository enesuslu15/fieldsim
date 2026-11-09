# pytest plugin

Installing fieldsim registers a pytest plugin (no `conftest.py` needed).

## Fixtures

| Fixture | Gives you |
|---|---|
| `modbus_device` | The Modbus device of the simulation. |
| `opcua_device` | The OPC UA device of the simulation. |
| `s7_device` | The S7 PLC of the simulation (experimental). |
| `fieldsim` | The whole simulation: `fieldsim["name"]`, `fieldsim.devices`, `fieldsim.advance(10)`. |
| `make_modbus_device`, `make_opcua_device`, `make_s7_device` | Factories for ad-hoc devices, cleaned up after the test. |

Without any configuration the device fixtures give you a blank device:

- `modbus_device`: a Modbus TCP device whose whole address space is plain memory,
- `opcua_device`: an OPC UA server with no variables,
- `s7_device`: a PLC with DB1 (1 KiB) and 256 bytes of merkers.

If a simulation has several devices of the same protocol, pick one with
`@pytest.mark.fieldsim(..., device="name")`.

## Configuration

From most to least specific:

```python
@pytest.mark.fieldsim("plant.yaml", device="boiler", clock="virtual", free_ports=True)
def test_something(modbus_device): ...
```

```ini
# pytest.ini / pyproject [tool.pytest.ini_options]
fieldsim_config = tests/plant.yaml
fieldsim_clock = real            # or virtual
fieldsim_free_ports = true       # ignore configured ports (default)
```

`config` can be a path (looked up next to the test file first, then in the
rootdir) or a dict. Marks can be applied per module with `pytestmark`.

## Device handles

All fixtures return a thread-safe handle:

| | |
|---|---|
| `host`, `port`, `address` | Where to connect. |
| `endpoint`, `node_id(point)` | OPC UA endpoint URL and node ids. |
| `unit_id` | Modbus unit id to use. |
| `rack`, `slot`, `address_of(point)` | S7 connection parameters and Siemens addresses. |
| `get(point)`, `set(point, value)`, `release(point)` | Read and change values. |
| `fault(type, **params)` | Activate a fault; a context manager with `.hits`. |
| `clear_faults()` | Remove every fault. |
| `offline()`, `go_offline()`, `go_online()`, `online` | Outages. |
| `writes`, `wait_for_write(point, timeout)`, `clear_writes()` | What clients wrote. |
| `advance(seconds)`, `scenario_done` | Virtual time. |

`wait_for_write` returns writes in order, including ones that happened before
the call, so there is no race between your code writing and the test waiting.

## Sync and async tests

Devices run on their own event loop in a background thread, so the same
fixtures work for plain tests, asyncio and trio. In async tests use the
non-blocking variants so your client keeps running:

```python
async def test_writes_setpoint(opcua_device):
    await set_setpoint(opcua_device.endpoint, 80)
    event = await opcua_device.wait_for_write_async("Line1.Setpoint")
    await opcua_device.advance_async(10)
```

## Parallel runs

Devices listen on free ports by default, so `pytest -n auto` (pytest-xdist)
works out of the box.

## Speed

Modbus and S7 devices start in milliseconds. The first OPC UA device of a test
session builds OPC UA's standard address space (about half a second, longer
under a coverage tracer); later ones reuse a copy and start in a few hundred
milliseconds.
