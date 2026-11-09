"""Siemens S7 simulation, experimental (requires ``pip install fieldsim[s7]``)."""

try:
    import snap7  # noqa: F401
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise ImportError("S7 support needs the 's7' extra: pip install 'fieldsim[s7]'") from exc

from fieldsim.protocols.s7.datastore import S7DataStore, S7PointLayout, S7WriteEvent
from fieldsim.protocols.s7.device import S7Device

__all__ = ["S7DataStore", "S7Device", "S7PointLayout", "S7WriteEvent"]
