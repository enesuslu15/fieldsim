import asyncio
import random
import struct

import pytest
from asyncua import ua
from asyncua.ua.ua_binary import struct_to_binary

from fieldsim.protocols.opcua.uabinary import (
    HEADER,
    FramingError,
    RequestInfo,
    Sequencer,
    UaChunk,
    corrupt,
    parse_request,
    read_chunk,
    service_fault,
    status_code_value,
)


def msg(body: bytes, *, seq: int = 1, request_id: int = 7, chunk: bytes = b"F") -> UaChunk:
    payload = struct.pack("<IIII", 5, 9, seq, request_id) + body
    return UaChunk(bytearray(HEADER.pack(b"MSG", chunk, HEADER.size + len(payload)) + payload))


def read_request(handle: int = 42) -> UaChunk:
    request = ua.ReadRequest()
    request.RequestHeader.RequestHandle = handle
    return msg(struct_to_binary(request))


def opn(policy: bytes, seq: int = 3) -> UaChunk:
    payload = (
        struct.pack("<I", 0)
        + struct.pack("<i", len(policy))
        + policy
        + struct.pack("<i", -1)  # no certificate
        + struct.pack("<i", -1)  # no thumbprint
        + struct.pack("<II", seq, 1)
        + b"body"
    )
    return UaChunk(bytearray(HEADER.pack(b"OPN", b"F", HEADER.size + len(payload)) + payload))


def reader_with(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def test_chunk_fields():
    chunk = msg(b"xyz", seq=11, request_id=99)
    assert chunk.message_type == b"MSG"
    assert chunk.is_message
    assert chunk.is_final
    assert chunk.sequence_number == 11
    assert chunk.request_id == 99
    assert chunk.body_offset == 24
    assert not msg(b"", chunk=b"C").is_final


def test_open_secure_channel_offsets():
    chunk = opn(b"http://opcfoundation.org/UA/SecurityPolicy#None", seq=5)
    assert chunk.has_sequence
    assert chunk.sequence_number == 5
    assert bytes(chunk.data[chunk.body_offset :]) == b"body"


def test_hello_has_no_sequence():
    hello = UaChunk(bytearray(HEADER.pack(b"HEL", b"F", 8)))
    assert not hello.has_sequence
    Sequencer().stamp(hello)  # no-op


def test_sequencer_renumbers_continuously():
    seq = Sequencer()
    chunks = [msg(b"", seq=n) for n in (50, 51, 60, 61)]  # a gap, as after a dropped chunk
    for c in chunks:
        seq.stamp(c)
    assert [c.sequence_number for c in chunks] == [50, 51, 52, 53]


def test_sequencer_wraps():
    seq = Sequencer()
    first = msg(b"", seq=(1 << 32) - 1025)
    second = msg(b"")
    seq.stamp(first)
    seq.stamp(second)
    assert second.sequence_number == 1


async def test_read_chunk():
    data = bytes(read_request().data)
    chunk = await read_chunk(reader_with(data))
    assert bytes(chunk.data) == data
    with pytest.raises(FramingError, match="invalid message size"):
        await read_chunk(reader_with(HEADER.pack(b"MSG", b"F", 3)))


def test_parse_request():
    info = parse_request(read_request(handle=1234))
    assert info == RequestInfo("Read", 1234)
    assert parse_request(msg(b"\xff\xff")) == RequestInfo(None, 0)


def test_service_fault():
    request = read_request(handle=77)
    fault = service_fault(request, parse_request(request), "BadTimeout")
    assert fault.message_type == b"MSG"
    assert fault.request_id == request.request_id
    assert bytes(fault.data[8:16]) == bytes(request.data[8:16])
    _, _, size = HEADER.unpack_from(fault.data)
    assert size == len(fault.data)
    body = bytes(fault.data[fault.body_offset :])
    assert body.startswith(b"\x01\x00\x8d\x01")  # ServiceFault type id


def test_status_code_value():
    assert status_code_value("BadTimeout") == ua.StatusCodes.BadTimeout
    assert status_code_value(5) == 5
    with pytest.raises(ValueError, match="unknown OPC UA status code"):
        status_code_value("BadNope")


@pytest.mark.parametrize("mode", ["truncate", "garbage", "bad_length", "bit_flip"])
def test_corrupt(mode):
    chunk = read_request()
    good = bytes(chunk.data)
    bad = corrupt(chunk, mode, random.Random(1))
    assert bad != good
    if mode == "bit_flip":
        assert bad[: chunk.body_offset] == good[: chunk.body_offset]
    if mode == "bad_length":
        assert bad[8:] == good[8:]


def test_corrupt_unknown_mode():
    with pytest.raises(ValueError, match="not supported for OPC UA"):
        corrupt(read_request(), "bad_crc", random.Random())
