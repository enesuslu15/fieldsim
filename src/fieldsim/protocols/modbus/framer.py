"""Modbus TCP framing (MBAP header)."""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass

MBAP_HEADER = struct.Struct(">HHHB")
"""transaction id, protocol id, length (unit id + PDU), unit id"""

MAX_PDU_SIZE = 253


class FramingError(Exception):
    """The byte stream is not valid Modbus TCP; the connection should be dropped."""


@dataclass(frozen=True)
class Frame:
    transaction_id: int
    unit_id: int
    pdu: bytes


def encode_frame(frame: Frame) -> bytes:
    return MBAP_HEADER.pack(frame.transaction_id, 0, len(frame.pdu) + 1, frame.unit_id) + frame.pdu


async def read_frame(reader: asyncio.StreamReader) -> Frame:
    """Read exactly one frame. Raises ``asyncio.IncompleteReadError`` on EOF."""
    header = await reader.readexactly(MBAP_HEADER.size)
    transaction_id, protocol_id, length, unit_id = MBAP_HEADER.unpack(header)
    if protocol_id != 0:
        raise FramingError(f"protocol id {protocol_id} is not Modbus (0)")
    if not 2 <= length <= MAX_PDU_SIZE + 1:
        raise FramingError(f"invalid MBAP length {length}")
    pdu = await reader.readexactly(length - 1)
    return Frame(transaction_id, unit_id, pdu)
