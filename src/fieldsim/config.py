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
from collections.abc import Callable, Iterator, Mapping
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    ValidationError,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from fieldsim.core.types import (
    OpcUaType,
    Order,
    RegisterType,
    coerce,
    coerce_opcua,
    register_count,
)

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


# --------------------------------------------------------------------------- faults

OPCUA_SERVICES = (
    "Read",
    "Write",
    "Browse",
    "BrowseNext",
    "TranslateBrowsePathsToNodeIds",
    "RegisterNodes",
    "UnregisterNodes",
    "Call",
    "CreateSubscription",
    "ModifySubscription",
    "DeleteSubscriptions",
    "SetPublishingMode",
    "Publish",
    "Republish",
    "CreateMonitoredItems",
    "ModifyMonitoredItems",
    "DeleteMonitoredItems",
    "SetMonitoringMode",
    "HistoryRead",
    "HistoryUpdate",
    "GetEndpoints",
    "FindServers",
    "CreateSession",
    "ActivateSession",
    "CloseSession",
    "Cancel",
)
OpcUaService = Literal[OPCUA_SERVICES]  # type: ignore[valid-type]


class _Fault(_Model):
    probability: float = Field(
        default=1.0, ge=0, le=1, description="Chance that a matching request is affected."
    )
    function_codes: list[Annotated[int, Field(ge=1, le=127)]] | None = Field(
        default=None, description="Modbus: only affect these function codes (default: all)."
    )
    services: list[OpcUaService] | None = Field(
        default=None, description="OPC UA: only affect these services, e.g. [Read, Write]."
    )
    count: int | None = Field(
        default=None, ge=1, description="Remove the fault after it affected this many requests."
    )


class LatencyFault(_Fault):
    """Delay responses by ``delay`` plus a random ``0..jitter``."""

    type: Literal["latency"]
    delay: Duration
    jitter: Duration = 0.0


class DropFault(_Fault):
    """Never answer the request. With ``executed: true`` the request is still
    carried out and only the response is lost - the nasty case for retries."""

    type: Literal["drop"]
    executed: bool = False


class DisconnectFault(_Fault):
    """Close the client connection instead of answering."""

    type: Literal["disconnect"]


class ExceptionFault(_Fault):
    """Modbus: answer with an exception code instead of the real response."""

    type: Literal["exception"]
    code: int = Field(default=6, ge=1, le=255, description="6 = SERVER DEVICE BUSY")


class StatusFault(_Fault):
    """OPC UA: answer with a ServiceFault carrying a bad status code."""

    type: Literal["status"]
    code: str | int = Field(
        default="BadCommunicationError",
        description="StatusCode name such as 'BadTimeout', or its numeric value.",
    )

    @field_validator("code")
    @classmethod
    def _valid(cls, v: str | int) -> str | int:
        if isinstance(v, str) and not re.fullmatch(r"(Bad|Uncertain)[A-Za-z0-9]+", v):
            raise ValueError("status code names start with 'Bad' or 'Uncertain'")
        if isinstance(v, int) and not 0 < v <= 0xFFFFFFFF:
            raise ValueError("status code must be a 32-bit unsigned value")
        return v


CorruptMode = Literal["bit_flip", "truncate", "garbage", "bad_length", "transaction_id", "bad_crc"]

TCP_ONLY_CORRUPT_MODES = frozenset({"bad_length", "transaction_id"})
RTU_ONLY_CORRUPT_MODES = frozenset({"bad_crc"})
OPCUA_CORRUPT_MODES = frozenset({"bit_flip", "truncate", "garbage", "bad_length"})


class CorruptFault(_Fault):
    """Send a damaged response.

    ``bit_flip`` flips one random bit of the payload, ``truncate`` sends only part
    of the message, ``garbage`` replaces it with random bytes of the same length,
    ``bad_length`` breaks the length field (Modbus TCP, OPC UA),
    ``transaction_id`` (Modbus TCP) answers with the wrong transaction id and
    ``bad_crc`` (Modbus RTU) breaks the CRC.
    """

    type: Literal["corrupt"]
    mode: CorruptMode = "bit_flip"


FaultSpec = Annotated[
    LatencyFault | DropFault | DisconnectFault | ExceptionFault | StatusFault | CorruptFault,
    Field(discriminator="type"),
]

MODBUS_PROTOCOLS = frozenset({"modbus_tcp", "modbus_rtu_over_tcp"})


def check_fault(fault: FaultSpec, protocol: str) -> None:
    """Reject faults that cannot happen with ``protocol``."""
    if protocol in MODBUS_PROTOCOLS:
        if isinstance(fault, StatusFault):
            raise ValueError("'status' faults are for OPC UA; use 'exception' with Modbus")
        if fault.services is not None:
            raise ValueError("'services' filters OPC UA services; use 'function_codes' with Modbus")
        if isinstance(fault, CorruptFault):
            if protocol == "modbus_tcp" and fault.mode in RTU_ONLY_CORRUPT_MODES:
                raise ValueError(
                    f"corrupt mode {fault.mode!r} needs RTU framing (CRC); "
                    "use it with modbus_rtu_over_tcp"
                )
            if protocol == "modbus_rtu_over_tcp" and fault.mode in TCP_ONLY_CORRUPT_MODES:
                raise ValueError(f"corrupt mode {fault.mode!r} only exists in Modbus TCP framing")
    elif protocol == "opcua":
        if isinstance(fault, ExceptionFault):
            raise ValueError(
                "'exception' faults are Modbus exception codes; use 'status' with OPC UA"
            )
        if fault.function_codes is not None:
            raise ValueError("'function_codes' filters Modbus requests; use 'services' with OPC UA")
        if isinstance(fault, CorruptFault) and fault.mode not in OPCUA_CORRUPT_MODES:
            raise ValueError(
                f"corrupt mode {fault.mode!r} does not exist for OPC UA "
                f"(use one of: {', '.join(sorted(OPCUA_CORRUPT_MODES))})"
            )


# --------------------------------------------------------------------------- scenario


class ScenarioStep(_Model):
    """One timed action. Exactly one of ``set``, ``release``, ``fault`` or ``offline``."""

    at: Duration = Field(description="Time since the device started.")
    set: dict[str, bool | int | float | str] | None = Field(
        default=None, description="Point values to set (pauses their generators)."
    )
    release: list[str] | None = Field(
        default=None, description="Points to hand back to their generators."
    )
    fault: FaultSpec | None = Field(default=None, description="Fault to activate.")
    duration: Duration | None = Field(
        default=None, description="How long the fault stays active (default: forever)."
    )
    offline: Duration | None = Field(
        default=None, description="Stop listening and drop all clients for this long."
    )

    @model_validator(mode="after")
    def _check(self) -> ScenarioStep:
        actions = [
            a for a in ("set", "release", "fault", "offline") if getattr(self, a) is not None
        ]
        if len(actions) != 1:
            raise ValueError(
                "each step needs exactly one of 'set', 'release', 'fault', 'offline'"
                + (f" (got {', '.join(actions)})" if actions else "")
            )
        if self.duration is not None and self.fault is None:
            raise ValueError("'duration' only applies to 'fault' steps")
        if self.offline is not None and self.offline <= 0:
            raise ValueError("offline duration must be > 0")
        return self


ValueCheck = Callable[[Any], object]


def _check_behaviour(
    protocol: str,
    faults: list[FaultSpec],
    scenario: list[ScenarioStep],
    checks: Mapping[str, ValueCheck],
) -> None:
    """Validate faults and scenario steps against a device's points and protocol."""
    all_faults = list(faults)
    for i, step in enumerate(scenario):
        where = f"scenario[{i}]"
        for name, value in (step.set or {}).items():
            check = checks.get(name)
            if check is None:
                raise ValueError(f"{where}: unknown point {name!r}")
            try:
                check(value)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{where}: {name}: {exc}") from None
        for name in step.release or []:
            if name not in checks:
                raise ValueError(f"{where}: unknown point {name!r}")
        if step.fault is not None:
            all_faults.append(step.fault)
    for fault in all_faults:
        check_fault(fault, protocol)


def _check_bool(value: Any) -> bool:
    if isinstance(value, str):
        raise TypeError("expected a boolean, got a string")
    return bool(value)


# --------------------------------------------------------------------------- devices


class _DeviceBase(_Model):
    name: Name
    host: str = "127.0.0.1"
    update_interval: Duration = Field(
        default=0.1, description="How often generators refresh their points."
    )
    faults: list[FaultSpec] = Field(
        default_factory=list, description="Faults active from the moment the device starts."
    )
    scenario: list[ScenarioStep] = Field(
        default_factory=list, description="Timed actions, relative to device start."
    )
    seed: int | None = Field(
        default=None, description="Seed for fault randomness, for reproducible runs."
    )

    @field_validator("update_interval")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("update_interval must be > 0")
        return v


class ModbusDeviceSpec(_DeviceBase):
    """A Modbus server reachable over TCP (native Modbus TCP or RTU framing over TCP)."""

    protocol: Literal["modbus_tcp", "modbus_rtu_over_tcp"]
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
    registers: RegisterMap = Field(default_factory=RegisterMap)

    def point_names(self) -> list[str]:
        return [p.name for p in self.registers.all_points()]

    @model_validator(mode="after")
    def _check(self) -> ModbusDeviceSpec:
        checks: dict[str, ValueCheck] = {}
        for point in self.registers.all_points():
            if isinstance(point, _RegisterPoint):
                checks[point.name] = partial(coerce, data_type=point.type)
            else:
                checks[point.name] = _check_bool
        _check_behaviour(self.protocol, self.faults, self.scenario, checks)
        return self


ModbusTcpDevice = ModbusDeviceSpec
"""Backwards compatible alias."""

# --------------------------------------------------------------------------- OPC UA

SegmentName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_\-]*$", max_length=128)]


class OpcUaVariable(_Model):
    """A variable node. Its point name is the dotted path from the device root."""

    name: SegmentName
    type: OpcUaType = "Double"
    value: bool | int | float | str | None = Field(
        default=None, description="Initial value (default: false, 0 or empty string)."
    )
    writable: bool = Field(default=False, description="Allow clients to write the value.")
    description: str | None = None
    generator: GeneratorSpec | None = None
    node_id: str | None = Field(
        default=None,
        pattern=r"^(s=.+|i=\d+)$",
        description="Identifier in the device namespace, e.g. 's=Temp' or 'i=1001'. "
        "Default: s=<dotted path>.",
    )

    @model_validator(mode="after")
    def _check(self) -> OpcUaVariable:
        if self.value is not None:
            try:
                coerce_opcua(self.value, self.type)
            except TypeError as exc:
                raise ValueError(str(exc)) from None
        if self.generator is not None and self.type == "String":
            raise ValueError("String variables cannot have a generator")
        return self


class OpcUaFolder(_Model):
    """An object node grouping other nodes."""

    name: SegmentName
    description: str | None = None
    nodes: list[OpcUaNode] = Field(default_factory=list)


def _node_kind(value: Any) -> str:
    if isinstance(value, dict):
        return "folder" if "nodes" in value else "variable"
    return "folder" if isinstance(value, OpcUaFolder) else "variable"


OpcUaNode = Annotated[
    Annotated[OpcUaFolder, Tag("folder")] | Annotated[OpcUaVariable, Tag("variable")],
    Discriminator(_node_kind),
]
OpcUaFolder.model_rebuild()


def iter_opcua_variables(
    nodes: list[OpcUaNode], prefix: str = ""
) -> Iterator[tuple[str, OpcUaVariable]]:
    """Yield ``(dotted path, variable)`` for every variable below ``nodes``."""
    for node in nodes:
        path = f"{prefix}.{node.name}" if prefix else node.name
        if isinstance(node, OpcUaFolder):
            yield from iter_opcua_variables(node.nodes, path)
        else:
            yield path, node


class OpcUaDeviceSpec(_DeviceBase):
    """An OPC UA server (binary protocol, security mode None)."""

    protocol: Literal["opcua"]
    port: int = Field(default=4840, ge=0, le=65535, description="0 picks a free port.")
    path: str = Field(default="/", pattern=r"^/\S*$", description="Endpoint URL path.")
    namespace: str | None = Field(
        default=None,
        description="Namespace URI of the device's nodes. Default: urn:fieldsim:<name>",
    )
    nodes: list[OpcUaNode] = Field(default_factory=list)

    @property
    def namespace_uri(self) -> str:
        return self.namespace or f"urn:fieldsim:{self.name}"

    def endpoint(self, host: str | None = None, port: int | None = None) -> str:
        return f"opc.tcp://{host or self.host}:{self.port if port is None else port}{self.path}"

    def variables(self) -> list[tuple[str, OpcUaVariable]]:
        return list(iter_opcua_variables(self.nodes))

    def point_names(self) -> list[str]:
        return [path for path, _ in self.variables()]

    @model_validator(mode="after")
    def _check(self) -> OpcUaDeviceSpec:
        _check_unique_names(self.nodes, "nodes")
        node_ids: dict[str, str] = {}
        checks: dict[str, ValueCheck] = {}
        for path, var in self.variables():
            node_id = var.node_id or f"s={path}"
            if node_id in node_ids:
                raise ValueError(f"{path!r} and {node_ids[node_id]!r} share node id {node_id!r}")
            node_ids[node_id] = path
            checks[path] = partial(coerce_opcua, data_type=var.type)
        _check_behaviour(self.protocol, self.faults, self.scenario, checks)
        return self


def _check_unique_names(nodes: list[OpcUaNode], where: str) -> None:
    seen: set[str] = set()
    for node in nodes:
        if node.name in seen:
            raise ValueError(f"{where}: duplicate node name {node.name!r}")
        seen.add(node.name)
        if isinstance(node, OpcUaFolder):
            _check_unique_names(node.nodes, f"{where}.{node.name}")


DeviceSpec = Annotated[ModbusDeviceSpec | OpcUaDeviceSpec, Field(discriminator="protocol")]
"""Any kind of device."""

AnyDeviceSpec = ModbusDeviceSpec | OpcUaDeviceSpec


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
        loc = tuple(p for p in err["loc"] if p not in _UNION_TAGS)
        msg = err["msg"].removeprefix("Value error, ")
        messages.append(f"{_format_location(loc)}: {msg}")
    return messages


_UNION_TAGS = {
    # generators
    "constant",
    "sine",
    "ramp",
    "random_walk",
    "sequence",
    # faults
    "latency",
    "drop",
    "disconnect",
    "exception",
    "status",
    "corrupt",
    # devices and OPC UA nodes
    "modbus_tcp",
    "modbus_rtu_over_tcp",
    "opcua",
    "folder",
    "variable",
}


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
