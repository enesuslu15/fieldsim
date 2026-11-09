"""Command line interface: ``fieldsim run | validate | schema``."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from pathlib import Path
from typing import Annotated, Optional

import typer

from fieldsim import __version__
from fieldsim.config import ConfigError, SimulationConfig, load_config
from fieldsim.simulator import Simulator

app = typer.Typer(
    name="fieldsim",
    help="Virtual industrial devices for testing software without hardware.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)

FileArg = Annotated[
    Path, typer.Argument(exists=False, dir_okay=False, help="Simulation YAML file.")
]


def _version(value: bool) -> None:
    if value:
        typer.echo(f"fieldsim {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        Optional[bool],  # noqa: UP045 - typer needs Optional on 3.10
        typer.Option("--version", callback=_version, is_eager=True, help="Show version."),
    ] = None,
) -> None:
    """fieldsim command line."""


def _load(path: Path) -> SimulationConfig:
    try:
        return load_config(path)
    except ConfigError as exc:
        typer.echo(f"ERROR {exc}", err=True)
        raise typer.Exit(code=1) from None


def _describe(config: SimulationConfig) -> str:
    devices = len(config.devices)
    points = sum(len(d.point_names()) for d in config.devices)
    return f"{devices} device{'s' * (devices != 1)}, {points} point{'s' * (points != 1)}"


@app.command()
def validate(
    files: Annotated[list[Path], typer.Argument(help="One or more simulation files.")],
) -> None:
    """Check simulation files without starting anything."""
    failed = False
    for path in files:
        try:
            config = load_config(path)
        except ConfigError as exc:
            typer.echo(f"ERROR {exc}", err=True)
            failed = True
        else:
            typer.echo(f"OK    {path}: {_describe(config)}")
    if failed:
        raise typer.Exit(code=1)


@app.command()
def schema(
    output: Annotated[
        Optional[Path],  # noqa: UP045
        typer.Option("--output", "-o", help="Write to a file instead of stdout."),
    ] = None,
) -> None:
    """Print the JSON Schema of the simulation file format (for editor autocompletion)."""
    document = SimulationConfig.model_json_schema()
    document["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    document["title"] = "fieldsim simulation file"
    text = json.dumps(document, indent=2)
    if output is None:
        typer.echo(text)
    else:
        output.write_text(text + "\n", encoding="utf-8")
        typer.echo(f"wrote {output}")


TEMPLATES = {
    "modbus": """# fieldsim device file - see https://github.com/enesuslu15/fieldsim
version: 1
devices:
  - name: plc
    protocol: modbus_tcp        # or modbus_rtu_over_tcp
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
    faults: []                  # e.g. - { type: latency, delay: 50ms }
    scenario:
      - { at: 30s, set: { temperature: 120.0 } }
""",
    "opcua": """# fieldsim device file - see https://github.com/enesuslu15/fieldsim
version: 1
devices:
  - name: line
    protocol: opcua
    port: 4840
    nodes:
      - name: Line1
        nodes:
          - name: Speed
            type: Double
            generator: { type: sine, min: 0.5, max: 1.5, period: 20s }
          - { name: Running, type: Boolean, value: true }
          - { name: Setpoint, type: Int32, value: 100, writable: true }
          - { name: Product, type: String, value: "A-100", writable: true }
    faults: []                  # e.g. - { type: status, code: BadTimeout, services: [Read] }
    scenario:
      - { at: 60s, offline: 10s }
""",
    "s7": """# fieldsim device file - see https://github.com/enesuslu15/fieldsim
version: 1
devices:
  - name: press
    protocol: s7                # experimental
    port: 1102                  # 102 is the standard port but needs root on Linux
    rack: 0
    slot: 1
    data_blocks:
      - number: 1
        points:
          - name: pressure
            offset: 0
            type: REAL
            generator: { type: sine, min: 180, max: 220, period: 10s }
          - { name: cycles, offset: 4, type: DINT }
          - { name: running, offset: 8, bit: 0, type: BOOL, value: true }
          - { name: setpoint, offset: 10, type: INT, value: 200 }
    merkers:
      size: 16
    faults: []                  # e.g. - { type: latency, delay: 50ms, services: [Read] }
""",
}


@app.command()
def init(
    path: Annotated[Path, typer.Argument(help="File to create.")] = Path("fieldsim.yaml"),
    protocol: Annotated[
        str, typer.Option("--protocol", "-p", help="modbus, opcua or s7.")
    ] = "modbus",
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Write a starter simulation file."""
    template = TEMPLATES.get(protocol)
    if template is None:
        raise typer.BadParameter(
            f"unknown protocol {protocol!r}; choose from {', '.join(TEMPLATES)}",
            param_hint="--protocol",
        )
    if path.exists() and not force:
        typer.echo(f"ERROR {path} already exists (use --force to overwrite)", err=True)
        raise typer.Exit(code=1)
    path.write_text(template, encoding="utf-8")
    typer.echo(f"wrote {path} - start it with: fieldsim run {path}")


@app.command()
def check(
    file: FileArg,
    host: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option(help="Connect to this host instead of the configured ones."),
    ] = None,
    wait: Annotated[float, typer.Option(help="Keep retrying for up to this many seconds.")] = 0.0,
    timeout: Annotated[float, typer.Option(help="Timeout per connection attempt.")] = 1.0,
) -> None:
    """Exit 0 once every device in FILE accepts TCP connections (health check / CI wait)."""
    config = _load(file)
    deadline = time.monotonic() + wait
    pending = {d.name: (_connect_host(host or d.host), d.port) for d in config.devices}
    while True:
        for name, address in list(pending.items()):
            try:
                socket.create_connection(address, timeout=timeout).close()
            except OSError:
                continue
            typer.echo(f"OK    {name} {address[0]}:{address[1]}")
            del pending[name]
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    for name, (h, p) in pending.items():
        typer.echo(f"ERROR {name} not reachable on {h}:{p}", err=True)
    if pending:
        raise typer.Exit(code=1)


def _connect_host(host: str) -> str:
    return "127.0.0.1" if host in ("", "0.0.0.0") else "::1" if host == "::" else host


@app.command()
def run(
    file: FileArg,
    duration: Annotated[
        Optional[float],  # noqa: UP045
        typer.Option(help="Stop after this many seconds (default: run until Ctrl+C)."),
    ] = None,
    host: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option(help="Listen on this address for every device, e.g. 0.0.0.0 in Docker."),
    ] = None,
    log_level: Annotated[
        str, typer.Option("--log-level", "-l", help="debug, info, warning or error.")
    ] = "info",
) -> None:
    """Start every device in FILE and serve until interrupted."""
    level = getattr(logging, log_level.upper(), None)
    if not isinstance(level, int):
        raise typer.BadParameter(f"unknown log level {log_level!r}", param_hint="--log-level")
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if level > logging.DEBUG:
        logging.getLogger("asyncua").setLevel(logging.ERROR)  # very chatty at INFO

    config = _load(file)
    if host is not None:
        devices = [device.model_copy(update={"host": host}) for device in config.devices]
        config = config.model_copy(update={"devices": devices})
    try:
        asyncio.run(_serve(Simulator(config), duration))
    except KeyboardInterrupt:
        pass
    except ImportError as exc:
        typer.echo(f"ERROR {exc}", err=True)
        raise typer.Exit(code=1) from None
    except OSError as exc:
        typer.echo(f"ERROR cannot start devices: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo("stopped")


async def _serve(sim: Simulator, duration: float | None) -> None:
    async with sim:
        for device in sim:
            generated = len(device.generated_points)
            address = getattr(device, "endpoint", f"{device.host}:{device.port}")
            typer.echo(
                f"{device.name:<20} {device.protocol:<20} {address}"
                f"  ({len(device.store.names())} points, {generated} generated)"
            )
        typer.echo("serving - press Ctrl+C to stop" if duration is None else f"serving {duration}s")
        if duration is None:
            await asyncio.Event().wait()
        else:
            await asyncio.sleep(duration)
