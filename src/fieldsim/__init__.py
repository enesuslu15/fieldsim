"""fieldsim - virtual industrial devices for testing software without hardware."""

from fieldsim.background import BackgroundSimulator, DeviceHandle, FaultHandle
from fieldsim.config import ConfigError, SimulationConfig, load_config
from fieldsim.core.clock import RealClock, VirtualClock
from fieldsim.core.device import Device
from fieldsim.core.faults import ActiveFault
from fieldsim.simulator import Simulator, create_device

__version__ = "0.3.0"

__all__ = [
    "ActiveFault",
    "BackgroundSimulator",
    "ConfigError",
    "Device",
    "DeviceHandle",
    "FaultHandle",
    "RealClock",
    "SimulationConfig",
    "Simulator",
    "VirtualClock",
    "__version__",
    "create_device",
    "load_config",
]
