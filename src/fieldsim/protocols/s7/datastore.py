"""S7 memory areas (data blocks, merkers, inputs, outputs) addressable by point name."""

from __future__ import annotations

import struct
from collections.abc import Sequence
from dataclasses import dataclass

from fieldsim.config import S7Area
from fieldsim.core.datastore import PointNotFoundError, Value
from fieldsim.core.types import S7_FORMATS, S7Type, coerce_s7

AreaKey = tuple[str, int]
"""``(area code, number)``: ``("DB", 1)``, ``("MK", 0)``, ``("PE", 0)``, ``("PA", 0)``."""


@dataclass(frozen=True)
class S7PointLayout:
    name: str
    area: str
    number: int
    offset: int
    bit: int
    data_type: S7Type
    size: int

    @property
    def address(self) -> str:
        """Address in Siemens notation, e.g. ``DB1.DBW4``, ``M2.3``."""
        if self.area == "DB":
            if self.data_type == "BOOL":
                return f"DB{self.number}.DBX{self.offset}.{self.bit}"
            width = {1: "B", 2: "W", 4: "D", 8: "D"}[self.size]
            return f"DB{self.number}.DB{width}{self.offset}"
        letter = {"MK": "M", "PE": "I", "PA": "Q"}[self.area]
        if self.data_type == "BOOL":
            return f"{letter}{self.offset}.{self.bit}"
        width = {1: "B", 2: "W", 4: "D", 8: "D"}[self.size]
        return f"{letter}{width}{self.offset}"


@dataclass(frozen=True)
class S7WriteEvent:
    """A write performed by an S7 client, with the values of the points it touched."""

    area: str
    number: int
    start: int
    size: int
    points: tuple[str, ...]
    values: tuple[Value, ...]


class S7DataStore:
    """Byte buffers shared with the S7 server; named points are views over them.

    Clients read and write the buffers directly, exactly as on a PLC, so a
    client writing half of a REAL behaves like the real thing.
    """

    def __init__(self, areas: Sequence[tuple[str, int, S7Area]]):
        self.buffers: dict[AreaKey, bytearray] = {}
        self._points: dict[str, S7PointLayout] = {}
        for code, number, area in areas:
            self.buffers[(code, number)] = bytearray(area.byte_size)
            for point in area.points:
                layout = S7PointLayout(
                    name=point.name,
                    area=code,
                    number=number,
                    offset=point.offset,
                    bit=point.bit_index,
                    data_type=point.type,
                    size=point.size,
                )
                self._points[point.name] = layout
                self._write(layout, coerce_s7(point.value, point.type))

    def names(self) -> list[str]:
        return list(self._points)

    def point(self, name: str) -> S7PointLayout:
        try:
            return self._points[name]
        except KeyError:
            raise PointNotFoundError(name, self._points) from None

    def points(self) -> list[S7PointLayout]:
        return list(self._points.values())

    def get(self, name: str) -> Value:
        layout = self.point(name)
        buffer = self.buffers[(layout.area, layout.number)]
        if layout.data_type == "BOOL":
            return bool(buffer[layout.offset] >> layout.bit & 1)
        (value,) = struct.unpack_from(">" + S7_FORMATS[layout.data_type], buffer, layout.offset)
        return value  # type: ignore[no-any-return]

    def set(self, name: str, value: Value, *, clamp: bool = False) -> None:
        layout = self.point(name)
        self._write(layout, coerce_s7(value, layout.data_type, clamp=clamp))

    def _write(self, layout: S7PointLayout, value: bool | int | float) -> None:
        buffer = self.buffers[(layout.area, layout.number)]
        if layout.data_type == "BOOL":
            mask = 1 << layout.bit
            current = buffer[layout.offset]
            buffer[layout.offset] = current | mask if value else current & ~mask
        else:
            struct.pack_into(">" + S7_FORMATS[layout.data_type], buffer, layout.offset, value)

    def points_in(self, area: str, number: int, start: int, size: int) -> tuple[str, ...]:
        """Names of the points overlapping ``size`` bytes from ``start``."""
        end = start + size
        return tuple(
            p.name
            for p in self._points.values()
            if p.area == area
            and p.number == number
            and p.offset < end
            and start < p.offset + p.size
        )
