"""Numeric data types and their encoding into 16-bit registers.

Industrial protocols move data around as 16-bit words. A ``float32`` occupies two
words, an ``int64`` four, and vendors disagree about the order of bytes inside a
word and of words inside a value. This module is the single place that knows how
to turn Python numbers into words and back.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence
from typing import Literal, get_args

RegisterType = Literal[
    "int16", "uint16", "int32", "uint32", "int64", "uint64", "float32", "float64"
]
"""Data type of a register-backed point."""

Order = Literal["big", "little"]
"""Byte order inside a word, or word order inside a multi-word value."""

REGISTER_TYPES: tuple[RegisterType, ...] = get_args(RegisterType)

_STRUCT_FORMAT: dict[RegisterType, str] = {
    "int16": "h",
    "uint16": "H",
    "int32": "i",
    "uint32": "I",
    "int64": "q",
    "uint64": "Q",
    "float32": "f",
    "float64": "d",
}

INTEGER_RANGES: dict[RegisterType, tuple[int, int]] = {
    "int16": (-(2**15), 2**15 - 1),
    "uint16": (0, 2**16 - 1),
    "int32": (-(2**31), 2**31 - 1),
    "uint32": (0, 2**32 - 1),
    "int64": (-(2**63), 2**63 - 1),
    "uint64": (0, 2**64 - 1),
}

FLOAT32_MAX = 3.4028234663852886e38

Number = int | float


def register_count(data_type: RegisterType) -> int:
    """Number of 16-bit registers a value of ``data_type`` occupies."""
    return struct.calcsize(">" + _STRUCT_FORMAT[data_type]) // 2


def is_integer_type(data_type: RegisterType) -> bool:
    return data_type in INTEGER_RANGES


def _coerce_integer(value: Number, lo: int, hi: int, clamp: bool, label: str) -> int:
    if isinstance(value, float):
        if math.isnan(value):
            raise ValueError(f"NaN cannot be stored as {label}")
        if math.isinf(value):
            if not clamp:
                raise ValueError(f"{value} cannot be stored as {label}")
            return hi if value > 0 else lo
        if clamp:
            value = round(value)
        elif not value.is_integer():
            raise ValueError(f"{value} is not a whole number, but {label} requires one")
        value = int(value)
    if lo <= value <= hi:
        return value
    if clamp:
        return max(lo, min(hi, value))
    raise ValueError(f"{value} is out of range for {label} [{lo}, {hi}]")


def _coerce_float(value: Number, single: bool, clamp: bool, label: str) -> float:
    result = float(value)
    if single and math.isfinite(result) and abs(result) > FLOAT32_MAX:
        if clamp:
            return math.copysign(FLOAT32_MAX, result)
        raise ValueError(f"{value} is out of range for {label}")
    return result


def _as_number(value: object) -> Number:
    if isinstance(value, bool):
        return int(value)
    if not isinstance(value, int | float):
        raise TypeError(f"expected a number, got {type(value).__name__}")
    return value


def coerce(value: Number, data_type: RegisterType, *, clamp: bool = False) -> Number:
    """Validate ``value`` for ``data_type`` and return it in canonical form.

    With ``clamp=False`` (user supplied values) anything that does not fit raises
    :class:`ValueError`. With ``clamp=True`` (generator output) floats are rounded
    and out-of-range values are saturated to the type limits, the way a real
    device's ADC or scaling block would.
    """
    number = _as_number(value)
    if data_type in INTEGER_RANGES:
        lo, hi = INTEGER_RANGES[data_type]
        return _coerce_integer(number, lo, hi, clamp, data_type)
    return _coerce_float(number, data_type == "float32", clamp, data_type)


# --------------------------------------------------------------------------- OPC UA

OpcUaType = Literal[
    "Boolean",
    "SByte",
    "Byte",
    "Int16",
    "UInt16",
    "Int32",
    "UInt32",
    "Int64",
    "UInt64",
    "Float",
    "Double",
    "String",
]
"""OPC UA built-in data types supported for variables."""

OPCUA_INTEGER_RANGES: dict[str, tuple[int, int]] = {
    "SByte": (-(2**7), 2**7 - 1),
    "Byte": (0, 2**8 - 1),
    "Int16": (-(2**15), 2**15 - 1),
    "UInt16": (0, 2**16 - 1),
    "Int32": (-(2**31), 2**31 - 1),
    "UInt32": (0, 2**32 - 1),
    "Int64": (-(2**63), 2**63 - 1),
    "UInt64": (0, 2**64 - 1),
}

OpcUaValue = bool | int | float | str


def opcua_default(data_type: OpcUaType) -> OpcUaValue:
    """Initial value of a variable that has none configured."""
    if data_type == "Boolean":
        return False
    if data_type == "String":
        return ""
    if data_type in ("Float", "Double"):
        return 0.0
    return 0


def coerce_opcua(value: object, data_type: OpcUaType, *, clamp: bool = False) -> OpcUaValue:
    """Like :func:`coerce`, for OPC UA data types (including Boolean and String)."""
    if data_type == "String":
        if not isinstance(value, str):
            raise TypeError(f"expected a string, got {type(value).__name__}")
        return value
    if isinstance(value, str):
        raise TypeError(f"expected a number for {data_type}, got a string")
    if data_type == "Boolean":
        return bool(value)
    number = _as_number(value)
    if data_type in OPCUA_INTEGER_RANGES:
        lo, hi = OPCUA_INTEGER_RANGES[data_type]
        return _coerce_integer(number, lo, hi, clamp, data_type)
    return _coerce_float(number, data_type == "Float", clamp, data_type)


def encode(
    value: Number,
    data_type: RegisterType,
    byte_order: Order = "big",
    word_order: Order = "big",
) -> list[int]:
    """Encode ``value`` into a list of 16-bit register values."""
    raw = struct.pack(">" + _STRUCT_FORMAT[data_type], coerce(value, data_type))
    words = [raw[i : i + 2] for i in range(0, len(raw), 2)]
    if word_order == "little":
        words.reverse()
    if byte_order == "little":
        words = [w[::-1] for w in words]
    return [int.from_bytes(w, "big") for w in words]


def decode(
    words: Sequence[int],
    data_type: RegisterType,
    byte_order: Order = "big",
    word_order: Order = "big",
) -> Number:
    """Decode 16-bit register values back into a Python number."""
    expected = register_count(data_type)
    if len(words) != expected:
        raise ValueError(f"{data_type} needs {expected} registers, got {len(words)}")
    chunks = [w.to_bytes(2, "big") for w in words]
    if byte_order == "little":
        chunks = [c[::-1] for c in chunks]
    if word_order == "little":
        chunks.reverse()
    (value,) = struct.unpack(">" + _STRUCT_FORMAT[data_type], b"".join(chunks))
    return value  # type: ignore[no-any-return]


# --------------------------------------------------------------------------- Siemens S7

S7Type = Literal[
    "BOOL",
    "BYTE",
    "WORD",
    "DWORD",
    "SINT",
    "USINT",
    "INT",
    "UINT",
    "DINT",
    "UDINT",
    "REAL",
    "LREAL",
]
"""S7 elementary data types supported for points (all big-endian, as in the PLC)."""

S7_FORMATS: dict[str, str] = {
    "BYTE": "B",
    "WORD": "H",
    "DWORD": "I",
    "SINT": "b",
    "USINT": "B",
    "INT": "h",
    "UINT": "H",
    "DINT": "i",
    "UDINT": "I",
    "REAL": "f",
    "LREAL": "d",
}


def s7_size(data_type: S7Type) -> int:
    """Bytes occupied by a value of ``data_type`` (a BOOL uses one bit of one byte)."""
    if data_type == "BOOL":
        return 1
    return struct.calcsize(">" + S7_FORMATS[data_type])


def coerce_s7(value: object, data_type: S7Type, *, clamp: bool = False) -> bool | int | float:
    """Like :func:`coerce`, for S7 data types."""
    if isinstance(value, str):
        raise TypeError(f"expected a number for {data_type}, got a string")
    if data_type == "BOOL":
        return bool(value)
    number = _as_number(value)
    fmt = S7_FORMATS[data_type]
    if fmt in "fd":
        return _coerce_float(number, fmt == "f", clamp, data_type)
    bits = struct.calcsize(fmt) * 8
    lo, hi = (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1) if fmt.islower() else (0, 2**bits - 1)
    return _coerce_integer(number, lo, hi, clamp, data_type)
