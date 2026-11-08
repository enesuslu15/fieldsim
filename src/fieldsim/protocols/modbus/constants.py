"""Modbus function codes, exception codes and protocol limits."""

from __future__ import annotations

from enum import IntEnum


class FunctionCode(IntEnum):
    READ_COILS = 0x01
    READ_DISCRETE_INPUTS = 0x02
    READ_HOLDING_REGISTERS = 0x03
    READ_INPUT_REGISTERS = 0x04
    WRITE_SINGLE_COIL = 0x05
    WRITE_SINGLE_REGISTER = 0x06
    WRITE_MULTIPLE_COILS = 0x0F
    WRITE_MULTIPLE_REGISTERS = 0x10


class ExceptionCode(IntEnum):
    ILLEGAL_FUNCTION = 0x01
    ILLEGAL_DATA_ADDRESS = 0x02
    ILLEGAL_DATA_VALUE = 0x03
    SERVER_DEVICE_FAILURE = 0x04
    SERVER_DEVICE_BUSY = 0x06
    GATEWAY_PATH_UNAVAILABLE = 0x0A
    GATEWAY_TARGET_FAILED = 0x0B


class ModbusError(Exception):
    """Aborts request processing and is answered with a Modbus exception response."""

    def __init__(self, code: ExceptionCode, message: str = ""):
        self.code = code
        super().__init__(message or code.name)


# Limits from the Modbus Application Protocol Specification V1.1b3.
MAX_READ_BITS = 2000
MAX_READ_REGISTERS = 125
MAX_WRITE_BITS = 1968
MAX_WRITE_REGISTERS = 123
ADDRESS_SPACE = 65536
