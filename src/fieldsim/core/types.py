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


def coerce(value: Number, data_type: RegisterType, *, clamp: bool = False) -> Number:
    """Validate ``value`` for ``data_type`` and return it in canonical form.

    With ``clamp=False`` (user supplied values) anything that does not fit raises
    :class:`ValueError`. With ``clamp=True`` (generator output) floats are rounded
    and out-of-range values are saturated to the type limits, the way a real
    device's ADC or scaling block would.
    """
    if isinstance(value, bool):
        value = int(value)
    if not isinstance(value, int | float):
        raise TypeError(f"expected a number, got {type(value).__name__}")

    if data_type in INTEGER_RANGES:
        lo, hi = INTEGER_RANGES[data_type]
        if isinstance(value, float):
            if math.isnan(value):
                raise ValueError(f"NaN cannot be stored as {data_type}")
            if math.isinf(value):
                if not clamp:
                    raise ValueError(f"{value} cannot be stored as {data_type}")
                return hi if value > 0 else lo
            if clamp:
                value = round(value)
            elif not value.is_integer():
                raise ValueError(f"{value} is not a whole number, but {data_type} requires one")
            value = int(value)
        if lo <= value <= hi:
            return value
        if clamp:
            return max(lo, min(hi, value))
        raise ValueError(f"{value} is out of range for {data_type} [{lo}, {hi}]")

    result = float(value)
    if data_type == "float32" and math.isfinite(result) and abs(result) > FLOAT32_MAX:
        if clamp:
            return math.copysign(FLOAT32_MAX, result)
        raise ValueError(f"{value} is out of range for float32")
    return result


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
