import asyncio
import random

import pytest

from fieldsim.protocols.modbus.framer import (
    Frame,
    FramingError,
    RtuFramer,
    TcpFramer,
    crc16,
    with_crc,
)


def reader_with(data: bytes, *, eof: bool = True) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    if eof:
        reader.feed_eof()
    return reader


def test_crc16_known_vector():
    # Read holding register 0, quantity 1, unit 1 -> CRC 0x0A84 (sent low byte first)
    assert crc16(bytes.fromhex("010300000001")) == 0x0A84
    assert with_crc(bytes.fromhex("010300000001")).hex() == "010300000001840a"


# --------------------------------------------------------------------------- TCP


async def test_tcp_roundtrip():
    framer = TcpFramer()
    data = framer.encode(Frame(1, b"\x03\x00\x00\x00\x01", transaction_id=9))
    frame = await framer.read_frame(reader_with(data))
    assert frame == Frame(1, b"\x03\x00\x00\x00\x01", 9)


async def test_tcp_rejects_foreign_protocol():
    with pytest.raises(FramingError, match="protocol id"):
        await TcpFramer().read_frame(reader_with(b"\x00\x01\x00\x07\x00\x02\x01\x03"))


@pytest.mark.parametrize(
    "mode", ["bit_flip", "truncate", "garbage", "bad_length", "transaction_id"]
)
def test_tcp_corrupt_modes_change_the_frame(mode):
    framer = TcpFramer()
    good = framer.encode(Frame(1, b"\x03\x02\x00\x4b", 5))
    bad = framer.corrupt(good, mode, random.Random(0))
    assert bad != good
    if mode == "truncate":
        assert 0 < len(bad) < len(good)
    elif mode in ("bit_flip", "garbage"):
        assert len(bad) == len(good)
    if mode == "bit_flip":
        assert bad[:7] == good[:7]  # header untouched
        assert sum(bin(a ^ b).count("1") for a, b in zip(bad, good, strict=True)) == 1
    if mode == "transaction_id":
        assert bad[:2] == b"\x00\x06"
    if mode == "bad_length":
        assert bad[4:6] != good[4:6]


def test_tcp_rejects_rtu_only_mode():
    with pytest.raises(ValueError, match="not supported by Modbus TCP"):
        TcpFramer().corrupt(b"\x00" * 10, "bad_crc", random.Random())


# --------------------------------------------------------------------------- RTU


async def test_rtu_fixed_size_request():
    frame = await RtuFramer().read_frame(reader_with(with_crc(b"\x01\x03\x00\x00\x00\x01")))
    assert frame == Frame(1, b"\x03\x00\x00\x00\x01")


async def test_rtu_variable_size_request():
    request = with_crc(b"\x02\x10\x00\x00\x00\x02\x04\x00\x01\x00\x02")
    frame = await RtuFramer().read_frame(reader_with(request))
    assert frame is not None
    assert frame.unit_id == 2
    assert frame.pdu == request[1:-2]


async def test_rtu_back_to_back_requests():
    reader = reader_with(
        with_crc(b"\x01\x03\x00\x00\x00\x01") + with_crc(b"\x01\x01\x00\x00\x00\x08")
    )
    framer = RtuFramer()
    assert (await framer.read_frame(reader)).pdu[0] == 3  # type: ignore[union-attr]
    assert (await framer.read_frame(reader)).pdu[0] == 1  # type: ignore[union-attr]


async def test_rtu_bad_crc_is_discarded():
    bad = bytearray(with_crc(b"\x01\x03\x00\x00\x00\x01"))
    bad[-1] ^= 0xFF
    assert await RtuFramer().read_frame(reader_with(bytes(bad))) is None


async def test_rtu_unknown_function_code_reads_until_quiet():
    request = with_crc(b"\x01\x2b\x0e\x01\x00")
    reader = reader_with(request, eof=False)
    frame = await RtuFramer().read_frame(reader)
    assert frame == Frame(1, b"\x2b\x0e\x01\x00")


async def test_rtu_unknown_function_code_with_nothing_else():
    framer = RtuFramer()
    framer.unknown_request_gap = 0.01
    assert await framer.read_frame(reader_with(b"\x01\x2b", eof=False)) is None
    with pytest.raises(asyncio.IncompleteReadError):
        await framer.read_frame(reader_with(b"\x01\x2b", eof=True))


def test_rtu_encode():
    assert RtuFramer().encode(Frame(1, b"\x03\x02\x00\x4b")) == with_crc(b"\x01\x03\x02\x00\x4b")


def test_rtu_corrupt_modes():
    framer = RtuFramer()
    good = framer.encode(Frame(1, b"\x03\x02\x00\x4b"))
    flipped = framer.corrupt(good, "bit_flip", random.Random(3))
    assert flipped != good
    assert crc16(flipped[:-2]) == int.from_bytes(flipped[-2:], "little")  # CRC still valid
    bad_crc = framer.corrupt(good, "bad_crc", random.Random())
    assert bad_crc[:-2] == good[:-2]
    assert crc16(bad_crc[:-2]) != int.from_bytes(bad_crc[-2:], "little")
    assert len(framer.corrupt(good, "truncate", random.Random())) < len(good)
    with pytest.raises(ValueError, match="not supported by RTU"):
        framer.corrupt(good, "transaction_id", random.Random())
