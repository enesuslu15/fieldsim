"""YAML configuration schema.

A simulation file describes one or more virtual devices::

    version: 1
    devices:
      - name: boiler-plc
        protocol: modbus_tcp
        port: 5020
        registers:
          holding_registers:
            - name: temperature
              address: 0
              type: float32
              generator: {type: sine, min: 60, max: 90, period: 30s}

The models here are the public contract of the file format. Everything is
validated up front so that mistakes are reported with a precise location instead
of surfacing as a confusing protocol error at runtime.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from fieldsim.core.types import Order, RegisterType, coerce, register_count

# --------------------------------------------------------------------------- primitives

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$")
_DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_duration(value: Any) -> float:
    """Parse ``"500ms"``, ``"2s"``, ``"1.5m"``, ``"1h"`` or a plain number of seconds."""
    if isinstance(value, bool):
        raise ValueError("expected a duration, got a boolean")
    if isinstance(value, int | float):
        seconds = float(value)
    elif isinstance(value, str):
        match = _DURATION_RE.match(value)
        if not match:
            raise ValueError(f"invalid duration {value!r}; use e.g. '500ms', '2s', '1m' or '1h'")
        seconds = float(match.group(1)) * _DURATION_UNITS[match.group(2) or "s"]
    else:
        raise ValueError(f"expected a duration, got {type(value).__name__}")
    if seconds < 0:
        raise ValueError("duration must not be negative")
    return seconds


Duration = Annotated[
    float,
    BeforeValidator(parse_duration),
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "number", "minimum": 0},
                {"type": "string", "pattern": _DURATION_RE.pattern},
            ],
            "description": "Seconds, or a string such as '500ms', '2s', '1m', '1h'.",
        }
    ),
]

Name = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.\-]*$", max_length=128)]
Address = Annotated[int, Field(ge=0, le=65535)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- generators


class ConstantGenerator(_Model):
    """Always produces the same value."""

    type: Literal["constant"]
    value: float


class SineGenerator(_Model):
    """Oscillates between ``min`` and ``max``, starting at the midpoint."""

    type: Literal["sine"]
    min: float
    max: float
    period: Duration
    phase: Duration = 0.0

    @model_validator(mode="after")
    def _check(self) -> SineGenerator:
        if self.min > self.max:
            raise ValueError("min must be <= max")
        if self.period <= 0:
            raise ValueError("period must be > 0")
        return self


class RampGenerator(_Model):
    """Moves linearly from ``start`` to ``end`` over ``period``.

    ``sawtooth`` jumps back to ``start``, ``triangle`` walks back down, ``once``
    stops at ``end``.
    """

    type: Literal["ramp"]
    start: float
    end: float
    period: Duration
    mode: Literal["sawtooth", "triangle", "once"] = "sawtooth"

    @field_validator("period")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("period must be > 0")
        return v


class RandomWalkGenerator(_Model):
    """Adds a random step in ``[-step, step]`` on every update, bounded by min/max."""

    type: Literal["random_walk"]
    start: float
    step: float = Field(gt=0)
    min: float
    max: float
    seed: int | None = None

    @model_validator(mode="after")
    def _check(self) -> RandomWalkGenerator:
        if self.min > self.max:
            raise ValueError("min must be <= max")
        if not self.min <= self.start <= self.max:
            raise ValueError("start must be between min and max")
        return self


class SequenceGenerator(_Model):
    """Steps through ``values``, holding each one for ``interval``."""

    type: Literal["sequence"]
    values: list[float] = Field(min_length=1)
    interval: Duration
    repeat: bool = True

    @field_validator("interval")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("interval must be > 0")
        return v


GeneratorSpec = Annotated[
    ConstantGenerator | SineGenerator | RampGenerator | RandomWalkGenerator | SequenceGenerator,
    Field(discriminator="type"),
]

# --------------------------------------------------------------------------- points


class _Point(_Model):
    name: Name
    address: Address
    description: str | None = None
    generator: GeneratorSpec | None = None


class _BitPoint(_Point):
    value: bool = False


class _RegisterPoint(_Point):
    type: RegisterType = "uint16"
    value: int | float = 0
    byte_order: Order = "big"
    word_order: Order = "big"

    @property
    def count(self) -> int:
        return register_count(self.type)

    @model_validator(mode="after")
    def _check(self) -> _RegisterPoint:
        if self.address + self.count - 1 > 65535:
            raise ValueError(
                f"{self.type} at address {self.address} needs {self.count} registers "
                "and runs past address 65535"
            )
        coerce(self.value, self.type)  # raises ValueError with a readable message
        return self


class CoilPoint(_BitPoint):
    """Read/write bit (Modbus function codes 1, 5, 15)."""

    writable: bool = True


class DiscreteInputPoint(_BitPoint):
    """Read-only bit (Modbus function code 2)."""


class HoldingRegisterPoint(_RegisterPoint):
    """Read/write register (Modbus function codes 3, 6, 16)."""

    writable: bool = True


class InputRegisterPoint(_RegisterPoint):
    """Read-only register (Modbus function code 4)."""


AnyPoint = CoilPoint | DiscreteInputPoint | HoldingRegisterPoint | InputRegisterPoint


class RegisterMap(_Model):
    coils: list[CoilPoint] = Field(default_factory=list)
    discrete_inputs: list[DiscreteInputPoint] = Field(default_factory=list)
    holding_registers: list[HoldingRegisterPoint] = Field(default_factory=list)
    input_registers: list[InputRegisterPoint] = Field(default_factory=list)

    def all_points(self) -> Iterator[AnyPoint]:
        yield from self.coils
        yield from self.discrete_inputs
        yield from self.holding_registers
        yield from self.input_registers

    @model_validator(mode="after")
    def _check(self) -> RegisterMap:
        seen: dict[str, str] = {}
        for table in ("coils", "discrete_inputs", "holding_registers", "input_registers"):
            owners: dict[int, str] = {}
            for point in getattr(self, table):
                if point.name in seen:
                    raise ValueError(
                        f"duplicate point name {point.name!r} (in {seen[point.name]} and {table})"
                    )
                seen[point.name] = table
                count = point.count if isinstance(point, _RegisterPoint) else 1
                for address in range(point.address, point.address + count):
                    if address in owners:
                        raise ValueError(
                            f"{table}: {point.name!r} overlaps {owners[address]!r} "
                            f"at address {address}"
                        )
                    owners[address] = point.name
        return self


# --------------------------------------------------------------------------- devices


class ModbusTcpDevice(_Model):
    """A Modbus TCP server."""

    name: Name
    protocol: Literal["modbus_tcp"]
    host: str = "127.0.0.1"
    port: int = Field(default=502, ge=0, le=65535, description="0 picks a free port.")
    unit_id: int | None = Field(
        default=None,
        ge=0,
        le=255,
        description="Only answer requests for this unit id. Omit to answer every unit id.",
    )
    unmapped: Literal["error", "allow"] = Field(
        default="error",
        description=(
            "'error' answers requests touching undefined addresses with exception 02 "
            "(ILLEGAL DATA ADDRESS); 'allow' treats them as plain memory initialised to 0."
        ),
    )
    update_interval: Duration = Field(
        default=0.1, description="How often generators refresh their points."
    )
    registers: RegisterMap = Field(default_factory=RegisterMap)

    @field_validator("update_interval")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("update_interval must be > 0")
        return v


DeviceSpec = ModbusTcpDevice
"""Union of all device kinds. Grows as protocols are added."""


class SimulationConfig(_Model):
    """Root of a simulation file."""

    version: Literal[1] = 1
    devices: list[DeviceSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> SimulationConfig:
        names: set[str] = set()
        endpoints: dict[tuple[str, int], str] = {}
        for device in self.devices:
            if device.name in names:
                raise ValueError(f"duplicate device name {device.name!r}")
            names.add(device.name)
            if device.port:
                key = (device.host, device.port)
                if key in endpoints:
                    raise ValueError(
                        f"devices {endpoints[key]!r} and {device.name!r} both listen on "
                        f"{device.host}:{device.port}"
                    )
                endpoints[key] = device.name
        return self


# --------------------------------------------------------------------------- loading


class ConfigError(Exception):
    """Raised when a simulation file cannot be read or is invalid."""

    def __init__(self, message: str, *, source: str | None = None, errors: list[str] | None = None):
        self.source = source
        self.errors = errors or []
        lines = [f"{source}: {message}" if source else message]
        lines += [f"  - {e}" for e in self.errors]
        super().__init__("\n".join(lines))


def _format_location(loc: tuple[int | str, ...]) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else part
    return out or "(root)"


def _format_validation_error(exc: ValidationError) -> list[str]:
    messages = []
    for err in exc.errors(include_url=False):
        # Drop the discriminator tag pydantic inserts for tagged unions, it is noise.
        loc = tuple(p for p in err["loc"] if p not in _GENERATOR_TAGS)
        msg = err["msg"].removeprefix("Value error, ")
        messages.append(f"{_format_location(loc)}: {msg}")
    return messages


_GENERATOR_TAGS = {"constant", "sine", "ramp", "random_walk", "sequence"}


def parse_config(data: Any, *, source: str | None = None) -> SimulationConfig:
    """Validate already-parsed YAML/JSON data."""
    if not isinstance(data, dict):
        raise ConfigError("top level must be a mapping with a 'devices' key", source=source)
    try:
        return SimulationConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(
            "invalid configuration", source=source, errors=_format_validation_error(exc)
        ) from None


def load_config(path: str | Path) -> SimulationConfig:
    """Read and validate a simulation file."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read file: {exc.strerror}", source=str(path)) from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML: {exc}", source=str(path)) from None
    return parse_config(data, source=str(path))
