# Faults and outages

Real devices are slow, busy, unreachable and occasionally send garbage. Faults
let you reproduce all of that on purpose.

## Switching faults on

From a test, scoped to a block:

```python
with modbus_device.fault("latency", delay="300ms", jitter="50ms"):
    ...

with opcua_device.fault("status", code="BadTimeout", services=["Read"]) as fault:
    ...
assert fault.hits == 1  # how many requests it affected
```

From the device file, active from the start:

```yaml
faults:
  - { type: latency, delay: 20ms, jitter: 10ms }
  - { type: drop, probability: 0.01 }
```

Or at a moment in a [scenario](scenarios.md):

```yaml
scenario:
  - { at: 40s, fault: { type: exception, code: 6 }, duration: 5s }
```

## Fault types

| Type | Modbus | OPC UA | S7 | Effect |
|---|:-:|:-:|:-:|---|
| `latency` | ✓ | ✓ | ✓ | Delay the response by `delay` + random `0..jitter`. |
| `drop` | ✓ | ✓ | ✓ | No response. With `executed: true` the request is still carried out — the lost-acknowledgement case that breaks naive retries. |
| `disconnect` | ✓ | ✓ | ✓ | Close the connection instead of answering. |
| `exception` | ✓ | | | Answer with Modbus exception `code` (default 6, SERVER DEVICE BUSY). |
| `status` | | ✓ | | Answer with a ServiceFault carrying `code` (`BadTimeout`, `BadServerHalted`, ... or a number). |
| `corrupt` | ✓ | ✓ | ✓ | Damage the response (see below). |

## Narrowing a fault

Every fault accepts:

- `probability` — chance (0..1) that a matching request is affected.
- `count` — remove the fault after it affected this many requests.
- `function_codes` — Modbus only, e.g. `[6, 16]` for writes.
- `services` — OPC UA (`Read`, `Write`, `Browse`, `Publish`, `CreateSession`, ...)
  or S7 (`Read`, `Write`).

```python
# the second write is lost, after it was applied
modbus_device.fault("drop", executed=True, function_codes=[6, 16], count=1)
```

Random faults draw from a generator seeded with the device's `seed`, so a
failing run can be replayed exactly.

## Corrupted responses

| Mode | Modbus TCP | RTU over TCP | OPC UA | S7 | What happens |
|---|:-:|:-:|:-:|:-:|---|
| `bit_flip` | ✓ | ✓ | ✓ | ✓ | One random bit of the payload flips. On RTU the CRC is recomputed, so only a plausibility check catches it. |
| `truncate` | ✓ | ✓ | ✓ | ✓ | Only part of the message is sent. |
| `garbage` | ✓ | ✓ | ✓ | ✓ | Random bytes of the same length. |
| `bad_length` | ✓ | | ✓ | ✓ | The length field (MBAP, UA message size, TPKT) is wrong. |
| `transaction_id` | ✓ | | | | The response carries the wrong transaction id. |
| `bad_crc` | | ✓ | | | Correct data, broken CRC. |

Combinations that cannot exist (a CRC error on Modbus TCP, an exception code on
OPC UA) are rejected when the file is loaded, with a hint about what to use instead.

## Outages

`offline()` stops the device from listening and drops every connected client;
values and generators keep running, as on a device whose network link went down.
When the block ends the device listens on the same port again.

```python
with modbus_device.offline():
    assert not gateway.healthy()
assert gateway.healthy()
```

In a scenario: `- { at: 60s, offline: 10s }`.
