import math

import pytest

from fieldsim.core.types import (
    FLOAT32_MAX,
    REGISTER_TYPES,
    coerce,
    coerce_opcua,
    decode,
    encode,
    is_integer_type,
    opcua_default,
    register_count,
)


@pytest.mark.parametrize(
    ("data_type", "count"),
    [("int16", 1), ("uint16", 1), ("int32", 2), ("float32", 2), ("uint64", 4), ("float64", 4)],
)
def test_register_count(data_type, count):
    assert register_count(data_type) == count


def test_float32_big_endian_layout():
    # 21.5 == 0x41AC0000
    assert encode(21.5, "float32") == [0x41AC, 0x0000]


@pytest.mark.parametrize(
    ("byte_order", "word_order", "expected"),
    [
        ("big", "big", [0x1234, 0x5678]),  # ABCD
        ("big", "little", [0x5678, 0x1234]),  # CDAB
        ("little", "big", [0x3412, 0x7856]),  # BADC
        ("little", "little", [0x7856, 0x3412]),  # DCBA
    ],
)
def test_byte_and_word_order(byte_order, word_order, expected):
    words = encode(0x12345678, "uint32", byte_order, word_order)
    assert words == expected
    assert decode(words, "uint32", byte_order, word_order) == 0x12345678


@pytest.mark.parametrize("data_type", REGISTER_TYPES)
@pytest.mark.parametrize("byte_order", ["big", "little"])
@pytest.mark.parametrize("word_order", ["big", "little"])
def test_roundtrip(data_type, byte_order, word_order):
    value = -1234 if data_type.startswith(("int", "float")) else 1234
    words = encode(value, data_type, byte_order, word_order)
    assert len(words) == register_count(data_type)
    assert all(0 <= w <= 0xFFFF for w in words)
    assert decode(words, data_type, byte_order, word_order) == value


def test_decode_wrong_length():
    with pytest.raises(ValueError, match="needs 2 registers"):
        decode([1], "float32")


def test_is_integer_type():
    assert is_integer_type("int16")
    assert not is_integer_type("float32")


class TestCoerce:
    def test_bool_becomes_int(self):
        assert coerce(True, "uint16") == 1

    def test_whole_float_accepted_for_int(self):
        assert coerce(5.0, "int16") == 5
        assert isinstance(coerce(5.0, "int16"), int)

    def test_fraction_rejected_for_int(self):
        with pytest.raises(ValueError, match="whole number"):
            coerce(5.5, "int16")

    def test_fraction_rounded_when_clamping(self):
        assert coerce(5.6, "int16", clamp=True) == 6

    @pytest.mark.parametrize(("value", "data_type"), [(70000, "uint16"), (-1, "uint16")])
    def test_out_of_range(self, value, data_type):
        with pytest.raises(ValueError, match="out of range"):
            coerce(value, data_type)

    def test_out_of_range_clamped(self):
        assert coerce(70000, "uint16", clamp=True) == 65535
        assert coerce(-5, "uint16", clamp=True) == 0

    def test_nan_rejected_for_int(self):
        with pytest.raises(ValueError, match="NaN"):
            coerce(math.nan, "int16", clamp=True)

    def test_inf(self):
        with pytest.raises(ValueError, match="cannot be stored"):
            coerce(math.inf, "int16")
        assert coerce(math.inf, "int16", clamp=True) == 32767
        assert coerce(-math.inf, "int16", clamp=True) == -32768

    def test_float32_range(self):
        with pytest.raises(ValueError, match="float32"):
            coerce(1e40, "float32")
        assert coerce(-1e40, "float32", clamp=True) == -FLOAT32_MAX
        assert coerce(1e40, "float64") == 1e40

    def test_non_number(self):
        with pytest.raises(TypeError, match="expected a number"):
            coerce("12", "int16")  # type: ignore[arg-type]


class TestCoerceOpcUa:
    @pytest.mark.parametrize(
        ("value", "data_type", "expected"),
        [
            (True, "Boolean", True),
            (0, "Boolean", False),
            ("x", "String", "x"),
            (127, "SByte", 127),
            (255.0, "Byte", 255),
            (1.5, "Double", 1.5),
            (2, "Float", 2.0),
            (True, "Int32", 1),
        ],
    )
    def test_valid(self, value, data_type, expected):
        result = coerce_opcua(value, data_type)
        assert result == expected
        assert type(result) is type(expected)

    @pytest.mark.parametrize(
        ("value", "data_type", "error", "message"),
        [
            (128, "SByte", ValueError, "out of range for SByte"),
            (1.5, "Int16", ValueError, "whole number"),
            (1e40, "Float", ValueError, "out of range for Float"),
            (5, "String", TypeError, "expected a string"),
            ("5", "Int32", TypeError, "expected a number for Int32"),
            ("yes", "Boolean", TypeError, "expected a number for Boolean"),
            (None, "Double", TypeError, "expected a number"),
        ],
    )
    def test_invalid(self, value, data_type, error, message):
        with pytest.raises(error, match=message):
            coerce_opcua(value, data_type)

    def test_clamp(self):
        assert coerce_opcua(300.4, "Byte", clamp=True) == 255
        assert coerce_opcua(-1e40, "Float", clamp=True) == -FLOAT32_MAX

    @pytest.mark.parametrize(
        ("data_type", "default"),
        [("Boolean", False), ("String", ""), ("Double", 0.0), ("Float", 0.0), ("UInt64", 0)],
    )
    def test_defaults(self, data_type, default):
        assert opcua_default(data_type) == default
