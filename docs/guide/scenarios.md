# Scenarios and virtual time

A scenario is a timeline of steps, relative to the moment the device starts.

```yaml
scenario:
  - { at: 30s, set: { temperature: 120.0, alarm: true } }
  - { at: 40s, fault: { type: exception, code: 6 }, duration: 5s }
  - { at: 60s, offline: 10s }
  - { at: 90s, release: [temperature] }
```

Each step does exactly one thing:

| Step | Effect |
|---|---|
| `set: {point: value, ...}` | Set values (their generators pause). |
| `release: [point, ...]` | Hand points back to their generators. |
| `fault: {...}` + optional `duration` | Activate a fault, for `duration` or until the device stops. |
| `offline: duration` | Outage: stop listening for `duration`, then come back on the same port. |

Point names and values are checked when the file is loaded.

## Virtual time

Waiting 90 seconds in a test is not an option. With the virtual clock, time only
moves when you say so — instantly, and identically on every run:

```python
@pytest.mark.fieldsim("boiler.yaml", clock="virtual")
def test_overheat_alarm(modbus_device):
    modbus_device.advance(29)
    assert not alarm_raised(modbus_device)
    modbus_device.advance(1)  # t = 30 s: temperature jumps to 120
    assert alarm_raised(modbus_device)
```

The clock is shared by all devices of a simulation, so multi-device scenarios
stay in step. Generators follow virtual time too. Network behaviour (latency
faults, socket timeouts) always uses real time.

Make it the default for a project with the ini option `fieldsim_clock = virtual`.

From Python:

```python
from fieldsim import Simulator, VirtualClock

clock = VirtualClock()
async with Simulator.from_yaml("boiler.yaml", clock=clock) as sim:
    await clock.advance(600)  # ten minutes, in milliseconds
```
