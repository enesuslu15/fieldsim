"""Command line interface: ``fieldsim run | validate | schema``."""

from __future__ import annotations

import asyncio
import json
import logging
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
    points = sum(len(list(d.registers.all_points())) for d in config.devices)
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


@app.command()
def run(
    file: FileArg,
    duration: Annotated[
        Optional[float],  # noqa: UP045
        typer.Option(help="Stop after this many seconds (default: run until Ctrl+C)."),
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

    config = _load(file)
    try:
        asyncio.run(_serve(Simulator(config), duration))
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        typer.echo(f"ERROR cannot start devices: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo("stopped")


async def _serve(sim: Simulator, duration: float | None) -> None:
    async with sim:
        for device in sim:
            generated = len(device.generated_points)
            typer.echo(
                f"{device.name:<20} {device.protocol:<11} {device.host}:{device.port}"
                f"  ({len(device.store.names())} points, {generated} generated)"
            )
        typer.echo("serving - press Ctrl+C to stop" if duration is None else f"serving {duration}s")
        if duration is None:
            await asyncio.Event().wait()
        else:
            await asyncio.sleep(duration)
