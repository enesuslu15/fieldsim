"""Modbus framings over a TCP stream.

* :class:`TcpFramer` - native Modbus TCP (MBAP header, no checksum).
* :class:`RtuFramer` - RTU frames tunnelled over TCP (unit id + PDU + CRC16), as
  spoken by many serial-to-Ethernet gateways.

Each framer also knows how to damage its own frames for fault injection.
"""

from __future__ import annotations

import asyncio
import logging
import random
import struct
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MBAP_HEADER = struct.Struct(">HHHB")
"""transaction id, protocol id, length (unit id + PDU), unit id"""

MAX_PDU_SIZE = 253


class FramingError(Exception):
    """The byte stream is not valid; the connection should be dropped."""


@dataclass(frozen=True)
class Frame:
    unit_id: int
    pdu: bytes
    transaction_id: int | None = None


def _flip_bit(data: bytes, start: int, end: int, rng: random.Random) -> bytearray:
    out = bytearray(data)
    if end > start:
        index = rng.randrange(start, end)
        out[index] ^= 1 << rng.randrange(8)
    return out


class Framer(ABC):
    name: str

    @abstractmethod
    async def read_frame(self, reader: asyncio.StreamReader) -> Frame | None:
        """Read one request. ``None`` means "discard and keep reading".

        Raises ``asyncio.IncompleteReadError`` on EOF and :class:`FramingError`
        when the stream cannot be resynchronised.
        """

    @abstractmethod
    def encode(self, frame: Frame) -> bytes: ...

    @abstractmethod
    def corrupt(self, data: bytes, mode: str, rng: random.Random) -> bytes:
        """Return a damaged copy of an encoded response."""

    def _corrupt_common(self, data: bytes, mode: str, rng: random.Random) -> bytes | None:
        if mode == "truncate":
            return data[: rng.randint(1, len(data) - 1)]
        if mode == "garbage":
            return rng.randbytes(len(data))
        return None


# --------------------------------------------------------------------------- Modbus TCP


class TcpFramer(Framer):
    name = "tcp"

    async def read_frame(self, reader: asyncio.StreamReader) -> Frame:
        header = await reader.readexactly(MBAP_HEADER.size)
        transaction_id, protocol_id, length, unit_id = MBAP_HEADER.unpack(header)
        if protocol_id != 0:
            raise FramingError(f"protocol id {protocol_id} is not Modbus (0)")
        if not 2 <= length <= MAX_PDU_SIZE + 1:
            raise FramingError(f"invalid MBAP length {length}")
        pdu = await reader.readexactly(length - 1)
        return Frame(unit_id, pdu, transaction_id)

    def encode(self, frame: Frame) -> bytes:
        return (
            MBAP_HEADER.pack(frame.transaction_id or 0, 0, len(frame.pdu) + 1, frame.unit_id)
            + frame.pdu
        )

    def corrupt(self, data: bytes, mode: str, rng: random.Random) -> bytes:
        common = self._corrupt_common(data, mode, rng)
        if common is not None:
            return common
        if mode == "bit_flip":
            return bytes(_flip_bit(data, MBAP_HEADER.size, len(data), rng))
        transaction_id, protocol_id, length, unit_id = MBAP_HEADER.unpack_from(data)
        if mode == "bad_length":
            length = (length + rng.randint(1, 8)) & 0xFFFF
        elif mode == "transaction_id":
            transaction_id = (transaction_id + 1) & 0xFFFF
        else:
            raise ValueError(f"corrupt mode {mode!r} is not supported by Modbus TCP")
        return MBAP_HEADER.pack(transaction_id, protocol_id, length, unit_id) + data[7:]


# --------------------------------------------------------------------------- RTU over TCP


def _make_crc_table() -> list[int]:
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        table.append(crc)
    return table


_CRC_TABLE = _make_crc_table()


def crc16(data: bytes) -> int:
    """Modbus CRC-16 (polynomial 0xA001, initial value 0xFFFF)."""
    crc = 0xFFFF
    for byte in data:
        crc = (crc >> 8) ^ _CRC_TABLE[(crc ^ byte) & 0xFF]
    return crc


def with_crc(data: bytes) -> bytes:
    return data + crc16(data).to_bytes(2, "little")


# Request PDU data length (after the function code) for fixed-size requests.
_FIXED_REQUEST_DATA = {0x01: 4, 0x02: 4, 0x03: 4, 0x04: 4, 0x05: 4, 0x06: 4}
_VARIABLE_REQUEST = {0x0F, 0x10}  # address(2) quantity(2) byte count(1) + data


class RtuFramer(Framer):
    """RTU framing has no length field; request size is derived from the function code."""

    name = "rtu"

    #: How long to wait for the rest of a request with an unknown function code.
    unknown_request_gap = 0.05

    async def read_frame(self, reader: asyncio.StreamReader) -> Frame | None:
        head = await reader.readexactly(2)
        fc = head[1]
        if fc in _FIXED_REQUEST_DATA:
            body = await reader.readexactly(_FIXED_REQUEST_DATA[fc] + 2)
        elif fc in _VARIABLE_REQUEST:
            fixed = await reader.readexactly(5)
            body = fixed + await reader.readexactly(fixed[4] + 2)
        else:
            # Unknown length: take whatever arrives before the line goes quiet.
            try:
                body = await asyncio.wait_for(
                    reader.read(MAX_PDU_SIZE + 2), self.unknown_request_gap
                )
            except asyncio.TimeoutError:
                body = b""
            if not body and reader.at_eof():
                raise asyncio.IncompleteReadError(head, None)
        frame = head + body
        if len(frame) < 4 or crc16(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
            # A real RTU slave silently ignores frames with a bad checksum.
            logger.debug("discarding RTU frame with bad CRC: %s", frame.hex(" "))
            return None
        return Frame(frame[0], frame[1:-2])

    def encode(self, frame: Frame) -> bytes:
        return with_crc(bytes([frame.unit_id]) + frame.pdu)

    def corrupt(self, data: bytes, mode: str, rng: random.Random) -> bytes:
        common = self._corrupt_common(data, mode, rng)
        if common is not None:
            return common
        if mode == "bit_flip":
            # Valid checksum over wrong data: the error only a plausibility check catches.
            return with_crc(bytes(_flip_bit(data, 1, len(data) - 2, rng))[:-2])
        if mode == "bad_crc":
            out = bytearray(data)
            out[-1] ^= 0xFF
            return bytes(out)
        raise ValueError(f"corrupt mode {mode!r} is not supported by RTU framing")


FRAMERS: dict[str, type[Framer]] = {
    "modbus_tcp": TcpFramer,
    "modbus_rtu_over_tcp": RtuFramer,
}
