"""Fault-injecting proxy for S7comm over ISO-on-TCP (RFC 1006).

Each message is a TPKT (version 3, length) carrying a COTP header and, for data
transfers, an S7 PDU::

    TPKT  03 00 LL LL
    COTP  LI F0 80                      (DT data, last unit)
    S7    32 ROSCTR 00 00 REF REF PL PL DL DL [ERRCLASS ERRCODE] params... data...

Jobs (ROSCTR 1) and their acknowledgements (ROSCTR 2/3) share a PDU
reference, which is how a fault decided for a request is applied to its
response. Unlike OPC UA there are no sequence numbers, so responses can be
dropped without breaking the connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import struct
from dataclasses import dataclass

from fieldsim.core.faults import FaultInjector, FaultPlan

logger = logging.getLogger(__name__)

TPKT = struct.Struct(">BBH")
S7_PROTOCOL_ID = 0x32
JOB, ACK, ACK_DATA = 1, 2, 3
SERVICES = {0x04: "Read", 0x05: "Write", 0xF0: "SetupCommunication"}


class FramingError(Exception):
    """Not a TPKT stream; the connection is dropped."""


async def read_tpkt(reader: asyncio.StreamReader) -> bytearray:
    header = await reader.readexactly(TPKT.size)
    version, _, length = TPKT.unpack(header)
    if version != 3 or length < TPKT.size + 3:
        raise FramingError(f"not a TPKT packet (version {version}, length {length})")
    return bytearray(header + await reader.readexactly(length - TPKT.size))


@dataclass(frozen=True)
class S7Header:
    offset: int
    """Index of the S7 protocol id inside the packet."""
    rosctr: int
    pdu_ref: int
    function: int | None

    @property
    def service(self) -> str:
        return SERVICES.get(self.function or -1, "Other")


def s7_header(packet: bytes | bytearray) -> S7Header | None:
    """Parse the S7 header of a COTP data packet, or ``None`` for anything else."""
    try:
        li = packet[4]
        if packet[5] != 0xF0:  # not COTP DT (connection request/confirm, ...)
            return None
        offset = 5 + li
        if packet[offset] != S7_PROTOCOL_ID:
            return None
        rosctr = packet[offset + 1]
        (pdu_ref, param_len) = struct.unpack_from(">HH", packet, offset + 4)
        params = offset + (12 if rosctr in (ACK, ACK_DATA) else 10)
        function = packet[params] if param_len and params < len(packet) else None
    except (IndexError, struct.error):
        return None
    return S7Header(offset, rosctr, pdu_ref, function)


def corrupt(packet: bytes | bytearray, mode: str, rng: random.Random) -> bytes:
    data = bytes(packet)
    if mode == "truncate":
        return data[: rng.randint(1, len(data) - 1)]
    if mode == "garbage":
        return rng.randbytes(len(data))
    if mode == "bad_length":
        out = bytearray(data)
        struct.pack_into(">H", out, 2, (len(data) + rng.randint(1, 8)) & 0xFFFF)
        return bytes(out)
    if mode == "bit_flip":
        header = s7_header(data)
        start = header.offset + 1 if header else TPKT.size
        out = bytearray(data)
        index = rng.randrange(start, len(out))
        out[index] ^= 1 << rng.randrange(8)
        return bytes(out)
    raise ValueError(f"corrupt mode {mode!r} is not supported for S7")


@dataclass
class _Pending:
    plan: FaultPlan
    service: str


class _Disconnect(Exception):
    pass


class S7Proxy:
    def __init__(self, *, host: str, port: int, faults: FaultInjector):
        self.faults = faults
        self.target: tuple[str, int] | None = None
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None
        self._clients: set[asyncio.Task[None]] = set()

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def serving(self) -> bool:
        return self._server is not None

    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("proxy already started")
        self._server = await asyncio.start_server(self._handle_client, self._host, self._port)
        self._host, self._port = self._server.sockets[0].getsockname()[:2]

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        clients = list(self._clients)
        for task in clients:
            task.cancel()
        await asyncio.gather(*clients, return_exceptions=True)
        await server.wait_closed()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._clients.add(task)
        upstream: asyncio.StreamWriter | None = None
        try:
            if self.target is None:  # pragma: no cover - only during start-up
                return
            up_reader, upstream = await asyncio.open_connection(*self.target)
            await self._relay(reader, writer, up_reader, upstream)
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            self._clients.discard(task)
            for w in (writer, upstream):
                if w is not None:
                    w.close()
                    with contextlib.suppress(ConnectionError, OSError):
                        await w.wait_closed()

    async def _relay(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        server_reader: asyncio.StreamReader,
        server_writer: asyncio.StreamWriter,
    ) -> None:
        pending: dict[int, _Pending] = {}

        async def requests() -> None:
            while True:
                packet = await read_tpkt(client_reader)
                header = s7_header(packet)
                if header is not None and header.rosctr == JOB:
                    plan = self.faults.plan(service=header.service)
                    if plan.disconnect:
                        if plan.delay:
                            await asyncio.sleep(plan.delay)
                        logger.info("fault: disconnecting S7 client on %s", header.service)
                        raise _Disconnect
                    if not plan.clean:
                        pending[header.pdu_ref] = _Pending(plan, header.service)
                    if plan.drop and not plan.execute:
                        logger.info("fault: dropped S7 %s request", header.service)
                        continue
                server_writer.write(bytes(packet))
                await server_writer.drain()

        async def responses() -> None:
            while True:
                packet = await read_tpkt(server_reader)
                header = s7_header(packet)
                entry = None
                if header is not None and header.rosctr in (ACK, ACK_DATA):
                    entry = pending.pop(header.pdu_ref, None)
                if entry is not None:
                    if entry.plan.drop:
                        logger.info("fault: dropped S7 %s response", entry.service)
                        continue
                    if entry.plan.delay:
                        await asyncio.sleep(entry.plan.delay)
                    if entry.plan.corrupt:
                        logger.info("fault: corrupting S7 response (%s)", entry.plan.corrupt)
                        packet = bytearray(corrupt(packet, entry.plan.corrupt, self.faults.rng))
                client_writer.write(bytes(packet))
                await client_writer.drain()

        tasks = {asyncio.create_task(requests()), asyncio.create_task(responses())}
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(
                    exc, _Disconnect | FramingError | asyncio.IncompleteReadError | OSError
                ):
                    raise exc
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
