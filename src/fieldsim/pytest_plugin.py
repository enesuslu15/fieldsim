"""pytest plugin: virtual devices as fixtures.

Installed automatically with fieldsim (``pytest11`` entry point)::

    def test_gateway_reads_temperature(modbus_device):
        modbus_device.set("temperature", 85.0)
        assert read_temperature(modbus_device.host, modbus_device.port) == 85.0

Configuration, from most to least specific:

* ``@pytest.mark.fieldsim("devices.yaml", device="plc", clock="virtual")``
* ini options ``fieldsim_config``, ``fieldsim_clock``, ``fieldsim_free_ports``
* otherwise a single Modbus TCP device whose whole address space is plain memory

Relative config paths are looked up next to the test file first, then in the
rootdir. Devices run on free ports by default so tests can run in parallel.

The fixtures are synchronous and the devices run in a background thread, so
they work with plain, asyncio and trio tests alike. In async tests use the
``*_async`` variants of blocking calls (``wait_for_write_async``,
``advance_async``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from fieldsim.background import BackgroundSimulator, ConfigSource, DeviceHandle
from fieldsim.config import ConfigError

DEFAULT_DEVICE: dict[str, Any] = {
    "name": "device",
    "protocol": "modbus_tcp",
    "port": 0,
    "unmapped": "allow",
}

MARKER_DOC = (
    "fieldsim(config=None, *, device=None, clock=None, free_ports=None): "
    "configure the fieldsim fixtures for this test (config: YAML path or dict)"
)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini(
        "fieldsim_config", "Default simulation file for the fieldsim fixtures.", default=""
    )
    parser.addini("fieldsim_clock", "'real' (default) or 'virtual'.", default="real")
    parser.addini(
        "fieldsim_free_ports",
        "Ignore configured ports and let the OS pick free ones (default: true).",
        type="bool",
        default=True,
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", MARKER_DOC)


def _find_file(request: pytest.FixtureRequest, value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    candidates = [Path(request.path).parent / path, Path(request.config.rootpath) / path]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]  # let the loader report a helpful "cannot read" error


def _options(request: pytest.FixtureRequest) -> dict[str, Any]:
    marker = request.node.get_closest_marker("fieldsim")
    kwargs: dict[str, Any] = dict(marker.kwargs) if marker else {}
    if marker and marker.args:
        kwargs.setdefault("config", marker.args[0])

    source: Any = kwargs.get("config")
    if source is None:
        ini_path = request.config.getini("fieldsim_config")
        source = ini_path or None
    if source is None:
        source = {"devices": [DEFAULT_DEVICE]}
    elif isinstance(source, str | Path):
        source = _find_file(request, source)

    free_ports = kwargs.get("free_ports")
    return {
        "config": source,
        "device": kwargs.get("device"),
        "clock": kwargs.get("clock") or request.config.getini("fieldsim_clock"),
        "free_ports": request.config.getini("fieldsim_free_ports")
        if free_ports is None
        else free_ports,
    }


def _start(source: ConfigSource, *, clock: Any, free_ports: bool) -> BackgroundSimulator:
    try:
        sim = BackgroundSimulator(source, clock=clock, free_ports=free_ports)
        sim.start()
    except (ConfigError, ValueError) as exc:
        pytest.fail(f"fieldsim: {exc}", pytrace=False)
    except OSError as exc:
        pytest.fail(f"fieldsim: cannot start devices: {exc}", pytrace=False)
    return sim


@pytest.fixture
def fieldsim(request: pytest.FixtureRequest) -> Iterator[BackgroundSimulator]:
    """The running simulator with every device of the configured file."""
    options = _options(request)
    sim = _start(options["config"], clock=options["clock"], free_ports=options["free_ports"])
    try:
        yield sim
    finally:
        sim.stop()


@pytest.fixture
def modbus_device(fieldsim: BackgroundSimulator, request: pytest.FixtureRequest) -> DeviceHandle:
    """The Modbus device of the simulation (pick one with ``mark.fieldsim(device=...)``)."""
    name = _options(request)["device"]
    if name is not None:
        return fieldsim[name]
    candidates = [d for d in fieldsim if d.protocol.startswith("modbus")]
    if len(candidates) != 1:
        names = ", ".join(d.name for d in candidates) or "none"
        pytest.fail(
            f"fieldsim: modbus_device needs exactly one Modbus device, found: {names}. "
            "Choose one with @pytest.mark.fieldsim(device='...').",
            pytrace=False,
        )
    return candidates[0]


@pytest.fixture
def make_modbus_device() -> Iterator[Callable[..., DeviceHandle]]:
    """Factory for ad-hoc devices, cleaned up after the test::

    def test_two_plcs(make_modbus_device):
        a = make_modbus_device(registers={"coils": [{"name": "run", "address": 0}]})
        b = make_modbus_device(protocol="modbus_rtu_over_tcp", unit_id=2)
    """
    started: list[BackgroundSimulator] = []

    def factory(
        spec: Mapping[str, Any] | None = None, *, clock: Any = "real", **fields: Any
    ) -> DeviceHandle:
        device = {**DEFAULT_DEVICE, "name": f"device{len(started) + 1}", **(spec or {}), **fields}
        sim = _start({"devices": [device]}, clock=clock, free_ports=True)
        started.append(sim)
        return sim[device["name"]]

    try:
        yield factory
    finally:
        for sim in reversed(started):
            sim.stop()
