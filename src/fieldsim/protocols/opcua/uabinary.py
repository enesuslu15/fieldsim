"""Just enough of the OPC UA binary protocol (UA-TCP, security mode None) to
inject faults between a client and the server.

A message chunk looks like this::

    MessageType(3) ChunkType(1) MessageSize(uint32 LE)          8 bytes
    SecureChannelId(4) + security header                         MSG/CLO: TokenId(4)
                                                                 OPN: policy uri, cert, thumbprint
    SequenceNumber(4) RequestId(4)                               sequence header
    body: TypeId (NodeId) + the service request/response         first chunk only
"""

from __future__ import annotations

import asyncio
import random
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache

from asyncua import ua
from asyncua.common.utils import Buffer
from asyncua.ua.ua_binary import nodeid_from_binary, struct_from_binary, struct_to_binary

HEADER = struct.Struct("<3scI")
MAX_MESSAGE_SIZE = 64 * 1024 * 1024
_WRAP_LIMIT = (1 << 32) - 1024


class FramingError(Exception):
    """The stream is not OPC UA binary; the connection should be dropped."""


@dataclass
class UaChunk:
    data: bytearray

    @property
    def message_type(self) -> bytes:
        return bytes(self.data[:3])

    @property
    def chunk_type(self) -> bytes:
        return bytes(self.data[3:4])

    @property
    def is_message(self) -> bool:
        return self.message_type == b"MSG"

    @property
    def is_final(self) -> bool:
        return self.chunk_type in (b"F", b"A")

    def _sequence_offset(self) -> int | None:
        kind = self.message_type
        if kind in (b"MSG", b"CLO"):
            return 16
        if kind == b"OPN":
            offset = 12
            for _ in range(3):  # policy uri, sender certificate, receiver thumbprint
                (length,) = struct.unpack_from("<i", self.data, offset)
                offset += 4 + max(length, 0)
            return offset
        return None  # HEL, ACK, ERR, RHE carry no sequence header

    @property
    def has_sequence(self) -> bool:
        return self._sequence_offset() is not None

    @property
    def sequence_number(self) -> int:
        offset = self._sequence_offset()
        assert offset is not None
        return int(struct.unpack_from("<I", self.data, offset)[0])

    @sequence_number.setter
    def sequence_number(self, value: int) -> None:
        offset = self._sequence_offset()
        assert offset is not None
        struct.pack_into("<I", self.data, offset, value)

    @property
    def request_id(self) -> int:
        offset = self._sequence_offset()
        assert offset is not None
        return int(struct.unpack_from("<I", self.data, offset + 4)[0])

    @property
    def body_offset(self) -> int:
        offset = self._sequence_offset()
        assert offset is not None
        return offset + 8


async def read_chunk(reader: asyncio.StreamReader) -> UaChunk:
    header = await reader.readexactly(HEADER.size)
    _, _, size = HEADER.unpack(header)
    if not HEADER.size <= size <= MAX_MESSAGE_SIZE:
        raise FramingError(f"invalid message size {size}")
    return UaChunk(bytearray(header + await reader.readexactly(size - HEADER.size)))


class Sequencer:
    """Issues the sequence numbers of one direction of a secure channel.

    The proxy drops and injects chunks, so it renumbers everything it forwards;
    receivers insist on numbers increasing by exactly one.
    """

    def __init__(self) -> None:
        self._next: int | None = None

    def stamp(self, chunk: UaChunk) -> None:
        if not chunk.has_sequence:
            return
        if self._next is None:
            self._next = chunk.sequence_number
        chunk.sequence_number = self._next
        self._next = self._next + 1 if self._next + 1 < _WRAP_LIMIT else 1


@lru_cache(maxsize=1)
def _service_names() -> dict[int, str]:
    suffix = "Request_Encoding_DefaultBinary"
    return {
        getattr(ua.ObjectIds, name): name[: -len(suffix)]
        for name in dir(ua.ObjectIds)
        if name.endswith(suffix)
    }


@dataclass(frozen=True)
class RequestInfo:
    service: str | None
    request_handle: int


def parse_request(chunk: UaChunk) -> RequestInfo:
    """Service name and request handle of the first chunk of a request."""
    try:
        buffer = Buffer(bytes(chunk.data[chunk.body_offset :]))
        type_id = nodeid_from_binary(buffer)
        header = struct_from_binary(ua.RequestHeader, buffer)
    except Exception:  # malformed or not a service request; pass it through untouched
        return RequestInfo(None, 0)
    service = _service_names().get(type_id.Identifier) if type_id.NamespaceIndex == 0 else None
    return RequestInfo(service, int(header.RequestHandle))


def status_code_value(code: str | int) -> int:
    if isinstance(code, int):
        return code
    value = getattr(ua.StatusCodes, code, None)
    if not isinstance(value, int):
        raise ValueError(f"unknown OPC UA status code {code!r}")
    return value


def service_fault(request: UaChunk, info: RequestInfo, code: str | int) -> UaChunk:
    """A ServiceFault response to ``request`` (sequence number assigned later)."""
    fault = ua.ServiceFault()
    fault.ResponseHeader.RequestHandle = info.request_handle
    fault.ResponseHeader.ServiceResult = ua.StatusCode(status_code_value(code))
    fault.ResponseHeader.Timestamp = datetime.now(timezone.utc)
    body = struct_to_binary(fault)
    # channel id + token id (8..16) and request id are taken from the request
    sequence = struct.pack("<II", 0, request.request_id)
    payload = bytes(request.data[8:16]) + sequence + body
    return UaChunk(bytearray(HEADER.pack(b"MSG", b"F", HEADER.size + len(payload)) + payload))


def corrupt(chunk: UaChunk, mode: str, rng: random.Random) -> bytes:
    data = bytes(chunk.data)
    if mode == "truncate":
        return data[: rng.randint(1, len(data) - 1)]
    if mode == "garbage":
        return rng.randbytes(len(data))
    if mode == "bad_length":
        out = bytearray(data)
        struct.pack_into("<I", out, 4, (len(data) + rng.randint(1, 8)) & 0xFFFFFFFF)
        return bytes(out)
    if mode == "bit_flip":
        out = bytearray(data)
        start = chunk.body_offset if chunk.has_sequence else HEADER.size
        if start < len(out):
            index = rng.randrange(start, len(out))
            out[index] ^= 1 << rng.randrange(8)
        return bytes(out)
    raise ValueError(f"corrupt mode {mode!r} is not supported for OPC UA")
