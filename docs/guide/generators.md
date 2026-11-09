# Generators

A generator gives a point a value that changes over time. It is sampled every
`update_interval` (default 100 ms) from the moment the device starts.

```yaml
- name: temperature
  address: 0
  type: float32
  generator: { type: sine, min: 60, max: 90, period: 30s }
```

| Type | Parameters | Behaviour |
|---|---|---|
| `constant` | `value` | Always the same value. |
| `sine` | `min`, `max`, `period`, `phase` | Starts at the midpoint, peaks after a quarter period. |
| `ramp` | `start`, `end`, `period`, `mode` | `sawtooth` jumps back, `triangle` walks back, `once` stops at `end`. |
| `random_walk` | `start`, `step`, `min`, `max`, `seed` | Adds a random step in `[-step, step]` each update, stays within bounds. |
| `sequence` | `values`, `interval`, `repeat` | Steps through a list, holding each value for `interval`. |

Generator output is fitted to the point type the way a real device's scaling
would: floats are rounded for integer types and values outside the type's
range are clamped. Boolean points are true for any non-zero value, so
`sequence` with `[0, 1]` makes a blinking bit.

## Overriding a generated value

Setting a value from a test **pauses** that point's generator, so the value
stays exactly as set:

```python
modbus_device.set("temperature", 120.0)  # generator paused
...
modbus_device.release("temperature")  # generator takes over again
```

Scenario `set` steps behave the same, and `release` steps hand points back.

## Reproducibility

`random_walk` takes a `seed`. With the [virtual clock](scenarios.md#virtual-time)
every generator produces exactly the same sequence on every run.
