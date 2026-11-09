"""OPC UA simulation (requires the ``opcua`` extra: ``pip install fieldsim[opcua]``)."""

try:
    import asyncua  # noqa: F401
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise ImportError(
        "OPC UA support needs the 'opcua' extra: pip install 'fieldsim[opcua]'"
    ) from exc

from fieldsim.protocols.opcua.datastore import OpcUaDataStore, OpcUaWriteEvent
from fieldsim.protocols.opcua.device import OpcUaDevice

__all__ = ["OpcUaDataStore", "OpcUaDevice", "OpcUaWriteEvent"]
