# Changelog

All notable changes to this project are documented here. The project follows
[Semantic Versioning](https://semver.org/); until 1.0 minor versions may change
the file format.

## [0.3.0] - unreleased

### Added

- **OPC UA devices** (`protocol: opcua`, install `fieldsim[opcua]`): a tree of folders and
  variables (Boolean, integer, Float, Double, String), generators, subscriptions, client
  writes in the write log, scenarios and outages, built on asyncua.
- OPC UA faults through a request-aware proxy: `latency`, `drop`, `disconnect`, `corrupt`
  and the new `status` fault (ServiceFault with any StatusCode), filtered by `services`.
- `opcua_device` and `make_opcua_device` pytest fixtures; `endpoint` and `node_id()` on
  device handles.
- CLI: `fieldsim init` (starter files), `fieldsim check` (health check / wait for devices)
  and `fieldsim run --host`.
- Docker image on `ghcr.io/enesuslu15/fieldsim` (amd64 + arm64) with a health check;
  docker compose and GitHub Actions examples.

### Changed

- The write log (`writes`, `wait_for_write`) moved to the common `Device` class.
- Scenario `set` steps accept strings (for OPC UA String variables); Modbus points reject them.
- CI actions updated to their Node 24 releases.

### Performance

- OPC UA's standard address space is built once per process and copied for later
  devices: device start-up drops from ~1.4 s to ~0.3 s, and from ~35 s to ~0.5 s under a
  coverage tracer.

## [0.2.0] - 2026-11-08

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
