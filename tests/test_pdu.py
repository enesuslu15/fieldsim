"""Byte-level tests of request handling, independent of any client library."""

import struct

import pytest

from fieldsim.protocols.modbus import ExceptionCode, ModbusDataStore
from fieldsim.protocols.modbus.pdu import handle_pdu, pack_bits, unpack_bits

from .conftest import modbus_spec


@pytest.fixture
def store():
    return ModbusDataStore(modbus_spec().registers)


def req(fc: int, fmt: str = "", *values: int) -> bytes:
    return bytes([fc]) + struct.pack(">" + fmt, *values)


def exc(fc: int, code: ExceptionCode) -> bytes:
    return bytes([fc | 0x80, code])


def test_pack_bits_lsb_first():
    bits = [True, False, True, True, False, False, False, False, True]
    assert pack_bits(bits) == b"\x0d\x01"
    assert unpack_bits(b"\x0d\x01", 9) == bits


def test_read_coils(store):
    assert handle_pdu(store, req(1, "HH", 0, 3)) == b"\x01\x01\x01"


def test_read_discrete_inputs(store):
    assert handle_pdu(store, req(2, "HH", 0, 1)) == b"\x02\x01\x01"


def test_read_holding_registers(store):
    assert handle_pdu(store, req(3, "HH", 0, 3)) == b"\x03\x06\x00\x4b\x41\xac\x00\x00"


def test_read_input_registers(store):
    assert handle_pdu(store, req(4, "HH", 0, 2)) == b"\x04\x04\x00\x01\x11\x70"


def test_write_single_coil(store):
    request = req(5, "HH", 1, 0xFF00)
    assert handle_pdu(store, request) == request
    assert store.get("valve") is True
    handle_pdu(store, req(5, "HH", 1, 0x0000))
    assert store.get("valve") is False


def test_write_single_coil_invalid_value(store):
    assert handle_pdu(store, req(5, "HH", 1, 0x1234)) == exc(5, ExceptionCode.ILLEGAL_DATA_VALUE)


def test_write_single_register(store):
    request = req(6, "HH", 0, 1234)
    assert handle_pdu(store, request) == request
    assert store.get("setpoint") == 1234


def test_write_multiple_coils(store):
    request = req(15, "HHBB", 0, 2, 1, 0b10)
    assert handle_pdu(store, request) == req(15, "HH", 0, 2)
    assert store.get("pump") is False
    assert store.get("valve") is True


def test_write_multiple_registers(store):
    request = req(16, "HHBHHH", 0, 3, 6, 10, 0x41AC, 0)
    assert handle_pdu(store, request) == req(16, "HH", 0, 3)
    assert store.get("setpoint") == 10
    assert store.get("temperature") == 21.5


def test_unknown_function_code(store):
    assert handle_pdu(store, b"\x2b\x0e\x01\x00") == exc(0x2B, ExceptionCode.ILLEGAL_FUNCTION)


def test_empty_pdu_is_a_bug(store):
    with pytest.raises(ValueError, match="empty"):
        handle_pdu(store, b"")


@pytest.mark.parametrize(
    ("request_pdu", "code"),
    [
        # wrong data length
        (req(3, "H", 0), ExceptionCode.ILLEGAL_DATA_VALUE),
        # quantity limits
        (req(1, "HH", 0, 0), ExceptionCode.ILLEGAL_DATA_VALUE),
        (req(1, "HH", 0, 2001), ExceptionCode.ILLEGAL_DATA_VALUE),
        (req(3, "HH", 0, 126), ExceptionCode.ILLEGAL_DATA_VALUE),
        # range past the end of the address space
        (req(3, "HH", 65535, 2), ExceptionCode.ILLEGAL_DATA_ADDRESS),
        # unmapped address
        (req(3, "HH", 100, 1), ExceptionCode.ILLEGAL_DATA_ADDRESS),
        (req(2, "HH", 0, 2), ExceptionCode.ILLEGAL_DATA_ADDRESS),
        # read-only
        (req(6, "HH", 3, 1), ExceptionCode.ILLEGAL_DATA_ADDRESS),
        (req(5, "HH", 2, 0xFF00), ExceptionCode.ILLEGAL_DATA_ADDRESS),
        # multiple writes: framing problems
        (req(15, "HH", 0, 1), ExceptionCode.ILLEGAL_DATA_VALUE),
        (req(15, "HHBB", 0, 9, 1, 0), ExceptionCode.ILLEGAL_DATA_VALUE),
        (req(15, "HHB", 0, 1, 1), ExceptionCode.ILLEGAL_DATA_VALUE),
        (req(15, "HHBBB", 65535, 9, 2, 0, 0), ExceptionCode.ILLEGAL_DATA_ADDRESS),
        (req(16, "HH", 0, 1), ExceptionCode.ILLEGAL_DATA_VALUE),
        (req(16, "HHBH", 0, 1, 3, 0), ExceptionCode.ILLEGAL_DATA_VALUE),
        (req(16, "HHB", 0, 1, 2), ExceptionCode.ILLEGAL_DATA_VALUE),
        (req(16, "HHBHH", 65535, 2, 4, 0, 0), ExceptionCode.ILLEGAL_DATA_ADDRESS),
        (req(16, "HHB", 0, 124, 248) + bytes(248), ExceptionCode.ILLEGAL_DATA_VALUE),
    ],
)
def test_exceptions(store, request_pdu, code):
    assert handle_pdu(store, request_pdu) == exc(request_pdu[0], code)


def test_unexpected_error_becomes_server_failure(store, monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(store, "read_registers", broken)
    assert handle_pdu(store, req(3, "HH", 0, 1)) == exc(3, ExceptionCode.SERVER_DEVICE_FAILURE)
    assert "unexpected error" in caplog.text
