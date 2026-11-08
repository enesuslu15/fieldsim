# fieldsim

**Virtual industrial devices for testing software without hardware.**

fieldsim starts a fake Modbus TCP device (OPC UA and S7 are on the roadmap) from a
YAML file, drives its values with generators and lets you test gateways, SCADA
connectors and edge apps in CI — no PLC on your desk required.

> Status: early alpha (`0.1.0a1`). The file format may still change.

## Install

```bash
pip install fieldsim
```

## Quick start

Describe a device:

```yaml
# boiler.yaml
version: 1
devices:
  - name: boiler-plc
    protocol: modbus_tcp
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
```

Run it:

```bash
fieldsim validate boiler.yaml
fieldsim run boiler.yaml
```

Or use it from Python:

```python
from fieldsim import Simulator

async with Simulator.from_yaml("boiler.yaml") as sim:
    plc = sim["boiler-plc"]
    plc.set("temperature", 85.0)  # pauses the generator for this point
    ...  # exercise your code against plc.host / plc.port
    plc.release("temperature")  # hand it back to the generator
```

## File format

| Key | Meaning |
|---|---|
| `devices[].protocol` | `modbus_tcp` |
| `host`, `port` | Listen address. `port: 0` picks a free port. |
| `unit_id` | Only answer this unit id (default: answer all). |
| `unmapped` | `error` (exception 02 for undefined addresses, default) or `allow` (plain memory). |
| `update_interval` | Generator refresh rate, e.g. `100ms`. |
| `registers.coils` / `discrete_inputs` | Bit points: `name`, `address`, `value`, `generator`, `writable` (coils). |
| `registers.holding_registers` / `input_registers` | `name`, `address`, `type`, `value`, `byte_order`, `word_order`, `generator`, `writable` (holding). |

Register types: `int16 uint16 int32 uint32 int64 uint64 float32 float64`.

Generators: `constant`, `sine`, `ramp` (`sawtooth` / `triangle` / `once`), `random_walk`, `sequence`.

Get a JSON Schema for editor autocompletion with `fieldsim schema -o fieldsim.schema.json`.

## Supported Modbus function codes

01 Read Coils · 02 Read Discrete Inputs · 03 Read Holding Registers · 04 Read Input Registers ·
05 Write Single Coil · 06 Write Single Register · 15 Write Multiple Coils · 16 Write Multiple Registers

## Development

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows; use .venv/bin/activate elsewhere
pip install -e ".[dev]"
pre-commit install
pytest --cov
```

## License

MIT
