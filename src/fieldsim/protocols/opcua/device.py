"""OPC UA device: an asyncua server behind a fault-injecting proxy."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import pickle
import threading
from datetime import datetime, timezone
from typing import Any

from asyncua import Node, Server, ua
from asyncua.common.callback import CallbackType, ServerItemCallback

from fieldsim.config import (
    FaultSpec,
    OpcUaDeviceSpec,
    OpcUaFolder,
    OpcUaNode,
    StatusFault,
    check_fault,
)
from fieldsim.core.clock import Clock
from fieldsim.core.device import Device
from fieldsim.core.generators import build_generator
from fieldsim.core.types import OpcUaValue
from fieldsim.protocols.opcua.datastore import OpcUaDataStore, OpcUaWriteEvent
from fieldsim.protocols.opcua.proxy import OpcUaProxy
from fieldsim.protocols.opcua.uabinary import status_code_value

logger = logging.getLogger(__name__)

_WILDCARD_HOSTS = {"", "0.0.0.0", "::"}


_standard_nodes: bytes | None = None
_standard_nodes_lock = threading.Lock()


def _use_cached_standard_address_space(server: Server) -> None:
    """Build OPC UA's standard address space once per process, then reuse a copy.

    Filling it means running ~10 000 lines of generated code: about 0.5 s
    normally and tens of seconds under a coverage tracer. Every device after
    the first one gets an unpickled copy of the pristine nodes instead (fast,
    since pickle is implemented in C). Any problem falls back to asyncua's own
    loading.
    """
    iserver = server.iserver
    original = iserver.load_standard_address_space

    async def load(shelf_file: Any = None) -> None:
        global _standard_nodes
        blob = _standard_nodes
        if blob is not None and not shelf_file:
            try:
                iserver.aspace._nodes = pickle.loads(blob)
                return
            except Exception:  # pragma: no cover - defensive
                logger.debug("cached address space unusable", exc_info=True)
        await original(shelf_file)
        if blob is None and not shelf_file:
            with _standard_nodes_lock:
                if _standard_nodes is None:
                    try:
                        _standard_nodes = pickle.dumps(
                            iserver.aspace._nodes, protocol=pickle.HIGHEST_PROTOCOL
                        )
                    except Exception:  # pragma: no cover - defensive
                        logger.debug("address space cannot be cached", exc_info=True)

    iserver.load_standard_address_space = load


def _node_id(identifier: str, namespace: int) -> ua.NodeId:
    kind, _, value = identifier.partition("=")
    if kind == "i":
        return ua.NodeId(int(value), namespace)
    return ua.NodeId(value, namespace)


class OpcUaDevice(Device[OpcUaDataStore]):
    """A virtual OPC UA server built from an :class:`OpcUaDeviceSpec`.

    Point names are dotted browse paths below the Objects folder, e.g.
    ``"Line1.Temperature"``. Clients find them under the device namespace
    (:attr:`namespace_index`) with the node ids returned by :meth:`node_id`.
    """

    protocol = "opcua"

    def __init__(self, spec: OpcUaDeviceSpec, *, clock: Clock | None = None):
        variables = spec.variables()
        store = OpcUaDataStore(variables)
        generators = {
            path: build_generator(var.generator)
            for path, var in variables
            if var.generator is not None
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
        self.proxy = OpcUaProxy(host=spec.host, port=spec.port, faults=self.faults)
        self.server: Server | None = None
        self.namespace_index = 0
        self._node_ids: dict[str, ua.NodeId] = {}
        self._paths: dict[ua.NodeId, str] = {}
        self._writes_in_flight: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ addresses

    @property
    def host(self) -> str:
        return self.proxy.host

    @property
    def port(self) -> int:
        return self.proxy.port

    @property
    def endpoint(self) -> str:
        """URL clients connect to, e.g. ``opc.tcp://127.0.0.1:4840/``."""
        host = "127.0.0.1" if self.host in _WILDCARD_HOSTS else self.host
        return self.spec.endpoint(host=host, port=self.port)

    def node_id(self, point: str) -> str:
        """Node id string of a point, e.g. ``"ns=2;s=Line1.Temperature"``."""
        info = self.store.info(point)
        index = self.namespace_index or 2
        return f"ns={index};{info.identifier}"

    # ------------------------------------------------------------------ lifecycle

    async def _start_server(self) -> None:
        await self.proxy.start()
        if self.server is None:
            try:
                await self._start_opcua()
            except BaseException:
                await self.proxy.stop()
                raise

    async def _stop_server(self) -> None:
        await self.proxy.stop()

    async def _shutdown(self) -> None:
        self.store.unbind()
        if self._writes_in_flight:
            await asyncio.gather(*self._writes_in_flight, return_exceptions=True)
        server, self.server = self.server, None
        if server is not None:
            # asyncua's stop() waits for its once-a-second clock task to wake up;
            # cancelling it first makes every device stop ~1 s faster.
            time_task = getattr(server.iserver, "time_task", None)
            if isinstance(time_task, asyncio.Task):
                time_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await time_task
                server.iserver.time_task = None
            with contextlib.suppress(Exception):
                await server.stop()

    async def _start_opcua(self) -> None:
        server = Server()
        _use_cached_standard_address_space(server)
        await server.init()
        advertised = self.host
        if advertised in _WILDCARD_HOSTS:
            import socket

            advertised = socket.gethostname()
        # Clients see the proxy's address; the real server listens privately.
        server.set_endpoint(self.spec.endpoint(host=advertised, port=self.port))
        server.socket_address = ("127.0.0.1", 0)
        server.set_server_name(f"fieldsim {self.name}")
        server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        self.namespace_index = await server.register_namespace(self.spec.namespace_uri)
        await self._build(server.nodes.objects, self.spec.nodes, "")
        server.iserver.subscribe_server_callback(CallbackType.PostWrite, self._on_post_write)
        await server.start()
        assert server.bserver is not None
        self.proxy.target = ("127.0.0.1", server.bserver.port)
        self.server = server
        self.store.bind(self._push)
        logger.info("opcua server %r listening on %s", self.name, self.endpoint)

    async def _build(self, parent: Node, nodes: list[OpcUaNode], prefix: str) -> None:
        ns = self.namespace_index
        for node in nodes:
            path = f"{prefix}.{node.name}" if prefix else node.name
            browse_name = ua.QualifiedName(node.name, ns)
            if isinstance(node, OpcUaFolder):
                child = await parent.add_folder(ua.NodeId(f"folder:{path}", ns), browse_name)
                await self._describe(child, node.description)
                await self._build(child, node.nodes, path)
                continue
            info = self.store.info(path)
            node_id = _node_id(info.identifier, ns)
            variant = ua.Variant(self.store.get(path), ua.VariantType[info.data_type])
            variable = await parent.add_variable(node_id, browse_name, variant)
            if info.writable:
                await variable.set_writable()
            await self._describe(variable, node.description)
            self._node_ids[path] = node_id
            self._paths[node_id] = path

    @staticmethod
    async def _describe(node: Node, description: str | None) -> None:
        if description:
            await node.write_attribute(
                ua.AttributeIds.Description,
                ua.DataValue(ua.Variant(ua.LocalizedText(description))),
            )

    # ------------------------------------------------------------------ values

    def _push(self, path: str, value: OpcUaValue) -> None:
        """Copy a changed value into the server (and so to subscribed clients)."""
        server = self.server
        if server is None:  # pragma: no cover - unbound before the server stops
            return
        now = datetime.now(timezone.utc)
        data_value = ua.DataValue(
            ua.Variant(value, ua.VariantType[self.store.info(path).data_type]),
            SourceTimestamp=now,
            ServerTimestamp=now,
        )
        task = asyncio.get_running_loop().create_task(
            server.iserver.aspace.write_attribute_value(
                self._node_ids[path], ua.AttributeIds.Value, data_value
            )
        )
        self._writes_in_flight.add(task)
        task.add_done_callback(self._writes_in_flight.discard)

    def _on_post_write(self, event: ServerItemCallback, _dispatcher: object) -> None:
        if not event.is_external:
            return
        points: list[str] = []
        values: list[OpcUaValue] = []
        requests = event.request_params.NodesToWrite
        for request, status in zip(requests, event.response_params, strict=False):
            path = self._paths.get(request.NodeId)
            if path is None or request.AttributeId != ua.AttributeIds.Value or not status.is_good():
                continue
            value = request.Value.Value.Value
            self.store.apply_client_write(path, value)
            points.append(path)
            values.append(value)
        if points:
            self._record_write(OpcUaWriteEvent(tuple(points), tuple(values)))

    # ------------------------------------------------------------------ faults

    def validate_fault(self, spec: FaultSpec) -> None:
        check_fault(spec, self.protocol)
        if isinstance(spec, StatusFault):
            status_code_value(spec.code)
