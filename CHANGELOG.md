# Changelog

All notable changes to this project are documented here. The project follows
[Semantic Versioning](https://semver.org/); until 1.0 minor versions may change
the file format.

## [0.2.0] - unreleased

### Added

- **Fault injection**: `latency` (with jitter), `drop` (optionally after executing the
  request), `disconnect`, `exception` and `corrupt` (`bit_flip`, `truncate`, `garbage`,
  `bad_length`, `transaction_id`, `bad_crc`). Faults can be limited by `probability`,
  `function_codes` and `count`, declared in YAML or switched on from code with
  `device.fault(...)`.
- **Outages**: `go_offline()` / `go_online()` / `offline()` stop listening and drop all
  clients, then come back on the same port.
- **Scenarios**: a timeline of `set`, `release`, `fault` (with `duration`) and `offline`
  steps per device.
- **Virtual clock**: play long scenarios instantly and deterministically with
  `clock="virtual"` and `advance()`.
- **Modbus RTU over TCP** (`protocol: modbus_rtu_over_tcp`) with CRC checking.
- **Write log**: `device.writes` and `wait_for_write(point)` to assert what a client sent.
- **pytest plugin**: `modbus_device`, `fieldsim` and `make_modbus_device` fixtures, the
  `@pytest.mark.fieldsim(...)` marker and `fieldsim_config` / `fieldsim_clock` /
  `fieldsim_free_ports` ini options.
- `BackgroundSimulator` to drive devices from synchronous code or other threads.
- Release workflow publishing to PyPI with Trusted Publishing.

### Changed

- `ModbusTcpDevice` is now `ModbusDeviceSpec` (the old name remains as an alias).

## [0.1.0a1]

- First preview: YAML device definitions, value generators, asyncio Modbus TCP server
  and the `fieldsim` CLI.
