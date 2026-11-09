# Adding a protocol

A new protocol is mostly a config model, a point store and a device class. The
generators, scenarios, faults, virtual clock, write log, CLI and pytest
fixtures then work without changes.

## 1. Config model

Add a spec to `fieldsim/config.py`, deriving from `_DeviceBase` (name, host,
update interval, faults, scenario, seed) with a `protocol` literal and the
protocol's own keys. Implement `point_names()` and validate faults and
scenario steps with `_check_behaviour`:

```python
class MqttDeviceSpec(_DeviceBase):
    protocol: Literal["mqtt"]
    port: int = Field(default=1883, ge=0, le=65535)
    topics: list[MqttTopic] = Field(default_factory=list)

    def point_names(self) -> list[str]:
        return [t.name for t in self.topics]

    @model_validator(mode="after")
    def _check(self) -> MqttDeviceSpec:
        checks = {t.name: partial(coerce, data_type=t.type) for t in self.topics}
        _check_behaviour(self.protocol, self.faults, self.scenario, checks)
        return self
```

Add it to the `DeviceSpec` union and teach `check_fault` which faults make
sense for the protocol.

## 2. Point store

Implement the `fieldsim.core.datastore.PointStore` protocol — `get`, `set`
(with `clamp` for generator output) and `names` — on top of whatever the
server needs (buffers, a dict, a node tree).

## 3. Device

Subclass `fieldsim.Device` and implement the network side:

```python
class MqttDevice(Device[MqttStore]):
    protocol = "mqtt"

    async def _start_server(self) -> None: ...  # start listening (also after an outage)
    async def _stop_server(self) -> None: ...  # stop listening (outages use this)
    async def _shutdown(self) -> None: ...  # release everything when stopped for good

    @property
    def host(self) -> str: ...

    @property
    def port(self) -> int: ...
```

Call `self._record_write(event)` for every client write (the event needs a
`points` tuple) and consult `self.faults.plan(...)` for every request — or put a
proxy in front of an existing server, as the OPC UA and S7 devices do.

## 4. Wire it up

- `create_device` in `fieldsim/simulator.py`,
- a fixture family in `fieldsim/pytest_plugin.py` (`mqtt_device`, `make_mqtt_device`),
- a starter template in `fieldsim init`,
- an optional dependency in `pyproject.toml` if the protocol needs a library.

## 5. Test it with an independent client

Every protocol in fieldsim is tested against a client library that fieldsim
does not control (pymodbus, asyncua's client, snap7's client), so the tests
check the protocol, not our own reading of it.
