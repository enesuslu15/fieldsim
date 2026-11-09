"""Fault-injecting TCP proxy in front of the OPC UA server.

Clients connect to the proxy; every connection is relayed to the internal
asyncua server. Requests are matched to their responses by request id, so a
fault decided when a Read request arrives is applied to that Read's response.

* ``latency`` delays the response, ``drop`` swallows it (or the request, unless
  ``executed``), ``status`` answers with a ServiceFault without bothering the
  server, ``corrupt`` damages the response and ``disconnect`` closes the client
  connection when the request arrives.
* Sequence numbers are rewritten in both directions so that dropped and
  injected chunks keep the secure channel valid.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from fieldsim.core.faults import FaultInjector, FaultPlan
from fieldsim.protocols.opcua.uabinary import (
    FramingError,
    Sequencer,
    UaChunk,
    corrupt,
    parse_request,
    read_chunk,
    service_fault,
)

logger = logging.getLogger(__name__)


@dataclass
class _Pending:
    plan: FaultPlan
    service: str | None
    swallow: bool = False
    """The request itself is not forwarded (``drop`` without ``executed``)."""


class _Disconnect(Exception):
    pass


class OpcUaProxy:
    def __init__(self, *, host: str, port: int, faults: FaultInjector):
        self.faults = faults
        #: Address of the real server; set once it is listening.
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

    @property
    def client_count(self) -> int:
        return len(self._clients)

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
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            if self.target is None:  # pragma: no cover - only during start-up
                return
            upstream_reader, upstream_writer = await asyncio.open_connection(*self.target)
            relay = _Relay(self.faults, reader, writer, upstream_reader, upstream_writer)
            await relay.run()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            self._clients.discard(task)
            for w in (writer, upstream_writer):
                if w is not None:
                    w.close()
                    with contextlib.suppress(ConnectionError, OSError):
                        await w.wait_closed()


class _Relay:
    def __init__(
        self,
        faults: FaultInjector,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        server_reader: asyncio.StreamReader,
        server_writer: asyncio.StreamWriter,
    ):
        self.faults = faults
        self.client_reader = client_reader
        self.client_writer = client_writer
        self.server_reader = server_reader
        self.server_writer = server_writer
        self.to_server = Sequencer()
        self.to_client = Sequencer()
        self.pending: dict[int, _Pending] = {}
        self._client_lock = asyncio.Lock()

    async def run(self) -> None:
        tasks = {
            asyncio.create_task(self._client_to_server()),
            asyncio.create_task(self._server_to_client()),
        }
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

    async def _client_to_server(self) -> None:
        while True:
            chunk = await read_chunk(self.client_reader)
            if chunk.is_message:
                pending = self.pending.get(chunk.request_id)
                if pending is None:
                    pending = await self._plan(chunk)
                    if pending is None:
                        continue  # answered by the proxy itself
                if pending.swallow:
                    if chunk.is_final:
                        self.pending.pop(chunk.request_id, None)
                    continue
            self.to_server.stamp(chunk)
            self.server_writer.write(bytes(chunk.data))
            await self.server_writer.drain()

    async def _plan(self, chunk: UaChunk) -> _Pending | None:
        info = parse_request(chunk)
        plan = self.faults.plan(service=info.service)
        if plan.disconnect:
            if plan.delay:
                await asyncio.sleep(plan.delay)
            logger.info("fault: disconnecting client on %s", info.service)
            raise _Disconnect
        if plan.status is not None and chunk.is_final:
            logger.info("fault: answering %s with %s", info.service, plan.status)
            await self._send_to_client(service_fault(chunk, info, plan.status), plan.delay)
            return None
        pending = _Pending(plan, info.service, swallow=plan.drop and not plan.execute)
        if not plan.clean or not chunk.is_final:
            # remember multi-chunk requests too, so later chunks are not re-planned
            self.pending[chunk.request_id] = pending
        return pending

    async def _server_to_client(self) -> None:
        while True:
            chunk = await read_chunk(self.server_reader)
            pending = self.pending.get(chunk.request_id) if chunk.is_message else None
            if pending is not None and chunk.is_final:
                del self.pending[chunk.request_id]
            if pending is not None and pending.plan.drop:
                logger.info("fault: dropped %s response", pending.service)
                continue
            plan = pending.plan if pending is not None else None
            await self._send_to_client(
                chunk, plan.delay if plan else 0.0, plan.corrupt if plan else None
            )

    async def _send_to_client(
        self, chunk: UaChunk, delay: float = 0.0, corrupt_mode: str | None = None
    ) -> None:
        if delay:
            await asyncio.sleep(delay)
        async with self._client_lock:
            self.to_client.stamp(chunk)
            data = bytes(chunk.data)
            if corrupt_mode is not None:
                logger.info("fault: corrupting response (%s)", corrupt_mode)
                data = corrupt(chunk, corrupt_mode, self.faults.rng)
            self.client_writer.write(data)
            await self.client_writer.drain()
