"""Siemens S7 device: a python-snap7 server behind a fault-injecting proxy."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from snap7.server import Server
from snap7.type import SrvArea

from fieldsim.config import FaultSpec, S7DeviceSpec, check_fault
from fieldsim.core.clock import Clock
from fieldsim.core.device import Device
from fieldsim.core.generators import build_generator
from fieldsim.protocols.s7.datastore import S7DataStore, S7WriteEvent
from fieldsim.protocols.s7.proxy import S7Proxy

logger = logging.getLogger(__name__)

EVENT_DATA_WRITE = 0x00040000
"""snap7 ``evcDataWrite``: EvtParam1 = area, 2 = DB number, 3 = start byte, 4 = size."""

_AREA_BY_CODE = {"DB": SrvArea.DB, "MK": SrvArea.MK, "PE": SrvArea.PE, "PA": SrvArea.PA}
_CODE_BY_AREA = {int(area): code for code, area in _AREA_BY_CODE.items()}


class S7Device(Device[S7DataStore]):
    """A virtual S7 PLC built from an :class:`S7DeviceSpec` (experimental).

    Clients connect with any S7 library (python-snap7, Sharp7, node-snap7, ...)
    to :attr:`host`, :attr:`port`, :attr:`rack` and :attr:`slot`.
    """

    protocol = "s7"

    def __init__(self, spec: S7DeviceSpec, *, clock: Clock | None = None):
        store = S7DataStore(spec.areas())
        generators = {
            point.name: build_generator(point.generator)
            for _, _, area in spec.areas()
            for point in area.points
            if point.generator is not None
        }
        super().__init__(
            spec.name,
            store,
            generators,
            update_interval=spec.update_interval,
            faults=spec.faults,
            scenario=spec.scenario,
            seed=spec.seed,
            clock=clock,
        )
        self.spec = spec
        self.proxy = S7Proxy(host=spec.host, port=spec.port, faults=self.faults)
        self.server: Server | None = None

    @property
    def host(self) -> str:
        return self.proxy.host

    @property
    def port(self) -> int:
        return self.proxy.port

    @property
    def rack(self) -> int:
        return self.spec.rack

    @property
    def slot(self) -> int:
        return self.spec.slot

    def address_of(self, point: str) -> str:
        """Siemens address of a point, e.g. ``"DB1.DBD0"``."""
        return self.store.point(point).address

    # ------------------------------------------------------------------ lifecycle

    async def _start_server(self) -> None:
        await self.proxy.start()
        if self.server is None:
            try:
                self._start_snap7()
            except BaseException:
                await self.proxy.stop()
                raise

    def _start_snap7(self) -> None:
        server = Server(log=False)
        for (code, number), buffer in self.store.buffers.items():
            server.register_area(_AREA_BY_CODE[code], number, buffer)
        loop = asyncio.get_running_loop()

        def on_event(event: Any) -> None:  # runs on a snap7 client thread
            loop.call_soon_threadsafe(
                self._on_event,
                event.EvtCode,
                event.EvtParam1,
                event.EvtParam2,
                event.EvtParam3,
                event.EvtParam4,
                event.EvtRetCode,
            )

        server.set_events_callback(on_event)
        server.start_to("127.0.0.1", 0)
        assert server.server_socket is not None
        self.proxy.target = ("127.0.0.1", server.server_socket.getsockname()[1])
        self.server = server
        logger.info("s7 device %r listening on %s:%s", self.name, self.host, self.port)

    async def _stop_server(self) -> None:
        await self.proxy.stop()

    async def _shutdown(self) -> None:
        server, self.server = self.server, None
        if server is not None:
            await asyncio.to_thread(server.stop)

    def _on_event(
        self, code: int, area: int, number: int, start: int, size: int, result: int
    ) -> None:
        if code != EVENT_DATA_WRITE or result != 0:
            return
        area_code = _CODE_BY_AREA.get(area)
        if area_code is None:
            return
        number = number if area_code == "DB" else 0
        points = self.store.points_in(area_code, number, start, size)
        values = tuple(self.store.get(name) for name in points)
        self._record_write(S7WriteEvent(area_code, number, start, size, points, values))

    def validate_fault(self, spec: FaultSpec) -> None:
        check_fault(spec, self.protocol)
