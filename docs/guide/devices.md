# Device files

A simulation file lists one or more devices. Devices of different protocols can
be mixed in one file and run together.

```yaml
version: 1
devices:
  - name: boiler-plc          # unique; used in tests: fieldsim["boiler-plc"]
    protocol: modbus_tcp      # modbus_tcp | modbus_rtu_over_tcp | opcua | s7
    host: 127.0.0.1           # listen address (0.0.0.0 in containers)
    port: 5020                # 0 = pick a free port
    update_interval: 100ms    # how often generators refresh values
    faults: []                # see "Faults and outages"
    scenario: []              # see "Scenarios and virtual time"
    seed: 42                  # makes random faults reproducible
    # ... protocol specific keys
```

Durations accept seconds (`2`, `0.5`) or strings (`500ms`, `2s`, `1.5m`, `1h`).

!!! tip "Editor autocompletion"
    `fieldsim schema -o fieldsim.schema.json` writes a JSON Schema. Add
    `# yaml-language-server: $schema=fieldsim.schema.json` at the top of a device
    file and VS Code (with the YAML extension) completes and checks it as you type.

Every key is listed in the [configuration reference](../reference/configuration.md).

## Modbus

```yaml
  - name: boiler-plc
    protocol: modbus_tcp
    port: 5020
    unit_id: 1             # only answer this unit id (default: any)
    unmapped: error        # error: exception 02 for undefined addresses; allow: plain memory
    registers:
      coils:
        - { name: pump_on, address: 0, value: true }
        - { name: interlock, address: 1, writable: false }
      discrete_inputs:
        - { name: flame_detected, address: 0 }
      holding_registers:
        - { name: temperature, address: 0, type: float32 }
        - { name: setpoint, address: 2, type: uint16, value: 75 }
        - { name: energy, address: 3, type: uint32, word_order: little }
      input_registers:
        - { name: runtime_hours, address: 0, type: uint32 }
```

| Register type | Registers | Range |
|---|---|---|
| `int16` / `uint16` | 1 | ±32767 / 0–65535 |
| `int32` / `uint32` / `float32` | 2 | |
| `int64` / `uint64` / `float64` | 4 | |

`byte_order` and `word_order` (`big` or `little`) cover the four layouts used by
real devices (ABCD, CDAB, BADC, DCBA). Points may not overlap; the same address
in different tables is fine.

`modbus_rtu_over_tcp` speaks RTU frames (unit id + PDU + CRC16) over a TCP
socket, as serial-to-Ethernet gateways do. Frames with a bad CRC are ignored,
just like on a real RTU slave.

Supported function codes: 01, 02, 03, 04, 05, 06, 15, 16. Unsupported ones
are answered with exception 01 (ILLEGAL FUNCTION).

## OPC UA

```yaml
  - name: packaging-line
    protocol: opcua
    port: 4840               # endpoint: opc.tcp://127.0.0.1:4840/
    path: /                  # endpoint path
    namespace: urn:example:packaging
    nodes:
      - name: Line1          # a folder: it has "nodes"
        description: Main packaging line
        nodes:
          - name: Speed      # a variable: point name "Line1.Speed"
            type: Double
            generator: { type: sine, min: 0.8, max: 1.2, period: 20s }
          - { name: Setpoint, type: Int32, value: 120, writable: true }
          - { name: Product, type: String, value: A-100, writable: true, node_id: "i=1001" }
```

- Variables live below the Objects folder. Their point name is the dotted path
  (`Line1.Speed`) and their node id `ns=<namespace index>;s=<path>` unless you
  set `node_id` (`s=...` or `i=...`). In tests, `opcua_device.node_id("Line1.Speed")`
  returns the full string.
- Types: `Boolean SByte Byte Int16 UInt16 Int32 UInt32 Int64 UInt64 Float Double String`.
- Clients may only write variables marked `writable: true`.
- Values changed by generators, scenarios or your test reach subscribed
  clients as data change notifications.
- Only security mode *None* is offered (no certificates, anonymous login).

## Siemens S7 (experimental)

```yaml
  - name: press-plc
    protocol: s7
    port: 1102               # 102 is standard but needs root on Linux
    rack: 0
    slot: 1
    data_blocks:
      - number: 10
        size: 64             # optional; default: just fits the points
        points:
          - { name: pressure, offset: 0, type: REAL }               # DB10.DBD0
          - { name: running, offset: 8, bit: 0, type: BOOL }        # DB10.DBX8.0
          - { name: setpoint, offset: 10, type: INT, value: 200 }   # DB10.DBW10
    merkers:                 # M area
      size: 16
      points:
        - { name: alarm, offset: 0, bit: 0, type: BOOL }            # M0.0
    inputs:  { size: 8 }     # I area
    outputs: { size: 8 }     # Q area
```

- Types: `BOOL BYTE WORD DWORD SINT USINT INT UINT DINT UDINT REAL LREAL`, all
  big-endian as in the PLC. `BOOL` points take a `bit` (0–7); several BOOLs can
  share a byte.
- Clients read and write the raw areas, so writing half a `REAL` behaves as on a
  real PLC. `s7_device.address_of("pressure")` gives the Siemens address.
- Any rack/slot is accepted on connect; the configured values are what your
  client should use.
