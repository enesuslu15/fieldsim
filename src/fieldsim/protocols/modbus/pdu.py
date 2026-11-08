"""Modbus PDU processing (transport independent).

``handle_pdu`` takes a request PDU (function code + data) and returns the
response PDU. Validation order follows the Modbus Application Protocol
specification: function code (01), quantity / framing (03), address range (02),
then execution (04).
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Callable, Sequence

from fieldsim.protocols.modbus.constants import (
    ADDRESS_SPACE,
    MAX_READ_BITS,
    MAX_READ_REGISTERS,
    MAX_WRITE_BITS,
    MAX_WRITE_REGISTERS,
    ExceptionCode,
    FunctionCode,
    ModbusError,
)
from fieldsim.protocols.modbus.datastore import ModbusDataStore, Table

logger = logging.getLogger(__name__)

_ADDR_QTY = struct.Struct(">HH")


def pack_bits(bits: Sequence[bool]) -> bytes:
    """Pack bits LSB-first, as Modbus does."""
    out = bytearray((len(bits) + 7) // 8)
    for i, bit in enumerate(bits):
        if bit:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def unpack_bits(data: bytes, count: int) -> list[bool]:
    return [bool(data[i // 8] >> (i % 8) & 1) for i in range(count)]


def exception_response(function_code: int, code: ExceptionCode | int) -> bytes:
    return bytes([(function_code | 0x80) & 0xFF, code])


def _addr_qty(data: bytes) -> tuple[int, int]:
    if len(data) != 4:
        raise ModbusError(ExceptionCode.ILLEGAL_DATA_VALUE, "expected 4 data bytes")
    address, quantity = _ADDR_QTY.unpack(data)
    return address, quantity


def _check_range(address: int, quantity: int, limit: int) -> None:
    if not 1 <= quantity <= limit:
        raise ModbusError(
            ExceptionCode.ILLEGAL_DATA_VALUE, f"quantity {quantity} not in 1..{limit}"
        )
    if address + quantity > ADDRESS_SPACE:
        raise ModbusError(ExceptionCode.ILLEGAL_DATA_ADDRESS, "range exceeds address space")


def _read_bits(table: Table) -> Handler:
    def handler(store: ModbusDataStore, fc: int, data: bytes) -> bytes:
        address, quantity = _addr_qty(data)
        _check_range(address, quantity, MAX_READ_BITS)
        payload = pack_bits(store.read_bits(table, address, quantity))
        return bytes([fc, len(payload)]) + payload

    return handler


def _read_registers(table: Table) -> Handler:
    def handler(store: ModbusDataStore, fc: int, data: bytes) -> bytes:
        address, quantity = _addr_qty(data)
        _check_range(address, quantity, MAX_READ_REGISTERS)
        words = store.read_registers(table, address, quantity)
        return bytes([fc, 2 * quantity]) + struct.pack(f">{quantity}H", *words)

    return handler


def _write_single_coil(store: ModbusDataStore, fc: int, data: bytes) -> bytes:
    address, value = _addr_qty(data)
    if value not in (0x0000, 0xFF00):
        raise ModbusError(ExceptionCode.ILLEGAL_DATA_VALUE, "coil value must be 0x0000 or 0xFF00")
    store.write_coils(address, [value == 0xFF00])
    return bytes([fc]) + data


def _write_single_register(store: ModbusDataStore, fc: int, data: bytes) -> bytes:
    address, value = _addr_qty(data)
    store.write_registers(address, [value])
    return bytes([fc]) + data


def _write_multiple_coils(store: ModbusDataStore, fc: int, data: bytes) -> bytes:
    if len(data) < 5:
        raise ModbusError(ExceptionCode.ILLEGAL_DATA_VALUE, "request too short")
    address, quantity = _ADDR_QTY.unpack_from(data)
    byte_count = data[4]
    if not 1 <= quantity <= MAX_WRITE_BITS or byte_count != (quantity + 7) // 8:
        raise ModbusError(ExceptionCode.ILLEGAL_DATA_VALUE, "bad quantity or byte count")
    if len(data) != 5 + byte_count:
        raise ModbusError(ExceptionCode.ILLEGAL_DATA_VALUE, "byte count does not match payload")
    _check_range(address, quantity, MAX_WRITE_BITS)
    store.write_coils(address, unpack_bits(data[5:], quantity))
    return bytes([fc]) + data[:4]


def _write_multiple_registers(store: ModbusDataStore, fc: int, data: bytes) -> bytes:
    if len(data) < 5:
        raise ModbusError(ExceptionCode.ILLEGAL_DATA_VALUE, "request too short")
    address, quantity = _ADDR_QTY.unpack_from(data)
    byte_count = data[4]
    if not 1 <= quantity <= MAX_WRITE_REGISTERS or byte_count != 2 * quantity:
        raise ModbusError(ExceptionCode.ILLEGAL_DATA_VALUE, "bad quantity or byte count")
    if len(data) != 5 + byte_count:
        raise ModbusError(ExceptionCode.ILLEGAL_DATA_VALUE, "byte count does not match payload")
    _check_range(address, quantity, MAX_WRITE_REGISTERS)
    store.write_registers(address, list(struct.unpack_from(f">{quantity}H", data, 5)))
    return bytes([fc]) + data[:4]


Handler = Callable[[ModbusDataStore, int, bytes], bytes]

HANDLERS: dict[int, Handler] = {
    FunctionCode.READ_COILS: _read_bits(Table.COILS),
    FunctionCode.READ_DISCRETE_INPUTS: _read_bits(Table.DISCRETE_INPUTS),
    FunctionCode.READ_HOLDING_REGISTERS: _read_registers(Table.HOLDING_REGISTERS),
    FunctionCode.READ_INPUT_REGISTERS: _read_registers(Table.INPUT_REGISTERS),
    FunctionCode.WRITE_SINGLE_COIL: _write_single_coil,
    FunctionCode.WRITE_SINGLE_REGISTER: _write_single_register,
    FunctionCode.WRITE_MULTIPLE_COILS: _write_multiple_coils,
    FunctionCode.WRITE_MULTIPLE_REGISTERS: _write_multiple_registers,
}


def handle_pdu(store: ModbusDataStore, pdu: bytes) -> bytes:
    """Process one request PDU and return the response PDU."""
    if not pdu:
        raise ValueError("empty PDU")
    fc = pdu[0]
    handler = HANDLERS.get(fc)
    if handler is None:
        logger.debug("unsupported function code 0x%02X", fc)
        return exception_response(fc, ExceptionCode.ILLEGAL_FUNCTION)
    try:
        return handler(store, fc, pdu[1:])
    except ModbusError as exc:
        logger.debug("fc 0x%02X -> exception %s: %s", fc, exc.code.name, exc)
        return exception_response(fc, exc.code)
    except Exception:
        logger.exception("unexpected error while handling fc 0x%02X", fc)
        return exception_response(fc, ExceptionCode.SERVER_DEVICE_FAILURE)
