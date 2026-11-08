"""Modbus memory: four tables of bits and 16-bit registers, addressable by point name."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from fieldsim.config import HoldingRegisterPoint, InputRegisterPoint, RegisterMap
from fieldsim.core.datastore import PointNotFoundError, Value
from fieldsim.core.types import Order, RegisterType, coerce, decode, encode, register_count
from fieldsim.protocols.modbus.constants import ExceptionCode, ModbusError


class Table(str, Enum):
    COILS = "coils"
    DISCRETE_INPUTS = "discrete_inputs"
    HOLDING_REGISTERS = "holding_registers"
    INPUT_REGISTERS = "input_registers"

    @property
    def is_bit(self) -> bool:
        return self in (Table.COILS, Table.DISCRETE_INPUTS)


@dataclass(frozen=True)
class PointLayout:
    name: str
    table: Table
    address: int
    count: int
    data_type: RegisterType | None
    byte_order: Order
    word_order: Order
    writable: bool

    @property
    def addresses(self) -> range:
        return range(self.address, self.address + self.count)


@dataclass(frozen=True)
class WriteEvent:
    """A write performed by a Modbus client."""

    table: Table
    address: int
    values: tuple[int, ...] | tuple[bool, ...]
    points: tuple[str, ...]


WriteListener = Callable[[WriteEvent], None]


class ModbusDataStore:
    """Single source of truth is the raw register/bit memory.

    Named points are views over that memory, so a client writing half of a
    ``float32`` behaves exactly as it would on real hardware.

    Not thread-safe: use it from the event loop that runs the server.
    """

    def __init__(self, registers: RegisterMap, *, unmapped: Literal["error", "allow"] = "error"):
        self._unmapped = unmapped
        self._bits: dict[Table, dict[int, bool]] = {Table.COILS: {}, Table.DISCRETE_INPUTS: {}}
        self._words: dict[Table, dict[int, int]] = {
            Table.HOLDING_REGISTERS: {},
            Table.INPUT_REGISTERS: {},
        }
        self._points: dict[str, PointLayout] = {}
        self._owners: dict[Table, dict[int, PointLayout]] = {t: {} for t in Table}
        self._listeners: list[WriteListener] = []

        for coil in registers.coils:
            self._add(Table.COILS, coil.name, coil.address, coil.value, writable=coil.writable)
        for di in registers.discrete_inputs:
            self._add(Table.DISCRETE_INPUTS, di.name, di.address, di.value, writable=False)
        for hr in registers.holding_registers:
            self._add(Table.HOLDING_REGISTERS, hr.name, hr.address, hr.value, hr, hr.writable)
        for ir in registers.input_registers:
            self._add(Table.INPUT_REGISTERS, ir.name, ir.address, ir.value, ir, writable=False)

    def _add(
        self,
        table: Table,
        name: str,
        address: int,
        value: Value,
        reg: HoldingRegisterPoint | InputRegisterPoint | None = None,
        writable: bool = True,
    ) -> None:
        layout = PointLayout(
            name=name,
            table=table,
            address=address,
            count=register_count(reg.type) if reg else 1,
            data_type=reg.type if reg else None,
            byte_order=reg.byte_order if reg else "big",
            word_order=reg.word_order if reg else "big",
            writable=writable,
        )
        self._points[name] = layout
        for a in layout.addresses:
            self._owners[table][a] = layout
        self._store(layout, value)

    # ------------------------------------------------------------------ by name

    def names(self) -> list[str]:
        return list(self._points)

    def point(self, name: str) -> PointLayout:
        try:
            return self._points[name]
        except KeyError:
            raise PointNotFoundError(name, self._points) from None

    def points(self) -> list[PointLayout]:
        return list(self._points.values())

    def get(self, name: str) -> Value:
        layout = self.point(name)
        if layout.data_type is None:
            return self._bits[layout.table][layout.address]
        words = [self._words[layout.table][a] for a in layout.addresses]
        return decode(words, layout.data_type, layout.byte_order, layout.word_order)

    def set(self, name: str, value: Value, *, clamp: bool = False) -> None:
        layout = self.point(name)
        if layout.data_type is not None:
            value = coerce(value, layout.data_type, clamp=clamp)
        self._store(layout, value)

    def _store(self, layout: PointLayout, value: Value) -> None:
        if layout.data_type is None:
            self._bits[layout.table][layout.address] = bool(value)
            return
        words = encode(value, layout.data_type, layout.byte_order, layout.word_order)
        table = self._words[layout.table]
        for a, w in zip(layout.addresses, words, strict=True):
            table[a] = w

    # ------------------------------------------------------------------ raw access

    def read_bits(self, table: Table, address: int, count: int) -> list[bool]:
        if not table.is_bit:
            raise ValueError(f"{table.value} is not a bit table")
        self._check(table, address, count, writing=False)
        bits = self._bits[table]
        return [bits.get(a, False) for a in range(address, address + count)]

    def read_registers(self, table: Table, address: int, count: int) -> list[int]:
        if table.is_bit:
            raise ValueError(f"{table.value} is not a register table")
        self._check(table, address, count, writing=False)
        words = self._words[table]
        return [words.get(a, 0) for a in range(address, address + count)]

    def write_coils(self, address: int, values: Sequence[bool]) -> None:
        points = self._check(Table.COILS, address, len(values), writing=True)
        bits = self._bits[Table.COILS]
        for offset, value in enumerate(values):
            bits[address + offset] = bool(value)
        self._notify(WriteEvent(Table.COILS, address, tuple(bool(v) for v in values), points))

    def write_registers(self, address: int, values: Sequence[int]) -> None:
        points = self._check(Table.HOLDING_REGISTERS, address, len(values), writing=True)
        words = self._words[Table.HOLDING_REGISTERS]
        for offset, value in enumerate(values):
            words[address + offset] = value & 0xFFFF
        self._notify(WriteEvent(Table.HOLDING_REGISTERS, address, tuple(values), points))

    def _check(self, table: Table, address: int, count: int, *, writing: bool) -> tuple[str, ...]:
        """Validate a raw access and return the names of the points it touches."""
        owners = self._owners[table]
        touched: dict[str, None] = {}
        for a in range(address, address + count):
            owner = owners.get(a)
            if owner is None:
                if self._unmapped == "error":
                    raise ModbusError(
                        ExceptionCode.ILLEGAL_DATA_ADDRESS, f"{table.value}[{a}] is not mapped"
                    )
                continue
            if writing and not owner.writable:
                raise ModbusError(
                    ExceptionCode.ILLEGAL_DATA_ADDRESS, f"{owner.name!r} is read-only"
                )
            touched[owner.name] = None
        return tuple(touched)

    # ------------------------------------------------------------------ events

    def add_write_listener(self, listener: WriteListener) -> Callable[[], None]:
        """Call ``listener`` after every client write. Returns a function that unsubscribes."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _notify(self, event: WriteEvent) -> None:
        for listener in list(self._listeners):
            listener(event)
