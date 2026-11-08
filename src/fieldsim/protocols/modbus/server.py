"""asyncio Modbus TCP server."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fieldsim.protocols.modbus.datastore import ModbusDataStore
from fieldsim.protocols.modbus.framer import Frame, FramingError, encode_frame, read_frame
from fieldsim.protocols.modbus.pdu import handle_pdu

logger = logging.getLogger(__name__)


class ModbusTcpServer:
    """Serves a :class:`ModbusDataStore` over Modbus TCP.

    Requests on one connection are processed in order; any number of clients
    can be connected at once.
    """

    def __init__(
        self,
        store: ModbusDataStore,
        *,
        host: str = "127.0.0.1",
        port: int = 502,
        unit_id: int | None = None,
    ):
        self.store = store
        self.unit_id = unit_id
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
            raise RuntimeError("server already started")
        self._server = await asyncio.start_server(self._handle_client, self._host, self._port)
        # With port 0 the OS picks a free port; report the real one.
        self._host, self._port = self._server.sockets[0].getsockname()[:2]
        logger.info("modbus tcp server listening on %s:%s", self._host, self._port)

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
        logger.info("modbus tcp server on %s:%s stopped", self._host, self._port)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._clients.add(task)
        peer = writer.get_extra_info("peername")
        logger.debug("client connected: %s", peer)
        try:
            while True:
                frame = await read_frame(reader)
                if self.unit_id is not None and frame.unit_id != self.unit_id:
                    logger.debug("ignoring request for unit %s", frame.unit_id)
                    continue
                response = handle_pdu(self.store, frame.pdu)
                writer.write(encode_frame(Frame(frame.transaction_id, frame.unit_id, response)))
                await writer.drain()
        except FramingError as exc:
            logger.warning("dropping %s: %s", peer, exc)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._clients.discard(task)
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()
            logger.debug("client disconnected: %s", peer)
