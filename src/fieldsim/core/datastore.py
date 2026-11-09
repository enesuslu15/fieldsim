"""Protocol-independent access to a device's points by name."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

Value = bool | int | float | str
"""A point value. Strings only exist on protocols that have them (OPC UA)."""


class PointNotFoundError(KeyError):
    """Raised when a point name does not exist on a device."""

    def __init__(self, name: str, available: Iterable[str] = ()):
        self.name = name
        self.available = sorted(available)
        super().__init__(name)

    def __str__(self) -> str:
        hint = f"; available: {', '.join(self.available)}" if self.available else ""
        return f"no point named {self.name!r}{hint}"


class PointStore(Protocol):
    """What generators, scenarios and tests need from a device's memory.

    Each protocol keeps its own native representation (Modbus: registers and
    bits, OPC UA: a node tree) and exposes it through this interface.
    """

    def get(self, name: str) -> Value: ...

    def set(self, name: str, value: Value, *, clamp: bool = False) -> None: ...

    def names(self) -> list[str]: ...
