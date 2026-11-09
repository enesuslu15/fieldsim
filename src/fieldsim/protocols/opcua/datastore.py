"""OPC UA variable values, addressable by dotted path."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from fieldsim.config import OpcUaVariable
from fieldsim.core.datastore import PointNotFoundError
from fieldsim.core.types import OpcUaType, OpcUaValue, coerce_opcua, opcua_default

Sink = Callable[[str, OpcUaValue], None]


@dataclass(frozen=True)
class VariableInfo:
    path: str
    data_type: OpcUaType
    writable: bool
    identifier: str
    """``s=...`` or ``i=...`` inside the device namespace."""


@dataclass(frozen=True)
class OpcUaWriteEvent:
    """A write performed by an OPC UA client (one Write service call)."""

    points: tuple[str, ...]
    values: tuple[OpcUaValue, ...]


class OpcUaDataStore:
    """Keeps the current value of every variable.

    While the server runs, every change is pushed to it through ``sink`` (which
    also notifies subscribed clients); client writes come back through
    :meth:`apply_client_write`. Not thread-safe: use it from the event loop.
    """

    def __init__(self, variables: Sequence[tuple[str, OpcUaVariable]]):
        self._info: dict[str, VariableInfo] = {}
        self._values: dict[str, OpcUaValue] = {}
        self._sink: Sink | None = None
        for path, var in variables:
            self._info[path] = VariableInfo(
                path=path,
                data_type=var.type,
                writable=var.writable,
                identifier=var.node_id or f"s={path}",
            )
            initial = opcua_default(var.type) if var.value is None else var.value
            self._values[path] = coerce_opcua(initial, var.type)

    def names(self) -> list[str]:
        return list(self._info)

    def info(self, name: str) -> VariableInfo:
        try:
            return self._info[name]
        except KeyError:
            raise PointNotFoundError(name, self._info) from None

    def variables(self) -> list[VariableInfo]:
        return list(self._info.values())

    def get(self, name: str) -> OpcUaValue:
        self.info(name)
        return self._values[name]

    def set(self, name: str, value: OpcUaValue, *, clamp: bool = False) -> None:
        info = self.info(name)
        coerced = coerce_opcua(value, info.data_type, clamp=clamp)
        self._values[name] = coerced
        if self._sink is not None:
            self._sink(name, coerced)

    def bind(self, sink: Sink) -> None:
        """Start forwarding changes to a running server."""
        self._sink = sink

    def unbind(self) -> None:
        self._sink = None

    def apply_client_write(self, name: str, value: OpcUaValue) -> None:
        """Record a value a client wrote (already stored by the server)."""
        self._values[name] = value
