import asyncio
import logging

from fieldsim.config import ScenarioStep
from fieldsim.core.clock import VirtualClock
from fieldsim.core.scenario import ScenarioRunner, build_events
from fieldsim.protocols.modbus import ModbusDevice

from .conftest import modbus_spec


def steps(*raw):
    return [ScenarioStep.model_validate(s) for s in raw]


class Recorder:
    def __init__(self):
        self.calls = []

    def set(self, name, value):
        self.calls.append(("set", name, value))

    def release(self, name):
        self.calls.append(("release", name))

    def add_fault(self, spec):
        self.calls.append(("fault", spec.type))

        class Handle:
            def remove(inner):
                self.calls.append(("unfault", spec.type))

        return Handle()

    async def go_offline(self):
        self.calls.append(("offline",))

    async def go_online(self):
        self.calls.append(("online",))


def test_build_events_expands_and_sorts():
    events = build_events(
        Recorder(),
        steps(
            {"at": "10s", "offline": "5s"},
            {"at": 2, "fault": {"type": "drop"}, "duration": 20},
            {"at": 2, "set": {"a": 1}},
            {"at": 0, "release": ["a"]},
        ),
    )
    assert [(e.time, e.description) for e in events] == [
        (0, "release a"),
        (2, "fault drop on"),
        (2, "set {'a': 1}"),
        (10, "offline"),
        (15, "online"),
        (22, "fault drop off"),
    ]


async def test_runner_follows_the_clock():
    clock = VirtualClock()
    target = Recorder()
    runner = ScenarioRunner(
        target,
        steps(
            {"at": 1, "set": {"a": 1, "b": True}},
            {"at": 2, "fault": {"type": "drop"}, "duration": 1},
            {"at": 5, "offline": 1},
            {"at": 7, "release": ["a"]},
        ),
        clock,
    )
    runner.start()
    runner.start()  # idempotent
    await clock.advance(0.5)
    assert target.calls == []
    await clock.advance(2.6)
    assert target.calls == [
        ("set", "a", 1),
        ("set", "b", True),
        ("fault", "drop"),
        ("unfault", "drop"),
    ]
    assert not runner.done
    assert runner.elapsed == 3.1
    await clock.advance(10)
    assert target.calls[-3:] == [("offline",), ("online",), ("release", "a")]
    assert runner.done
    await runner.wait()
    assert len(runner.history) == 6


async def test_empty_scenario_is_done_immediately():
    runner = ScenarioRunner(Recorder(), [], VirtualClock())
    runner.start()
    assert runner.done
    await runner.wait()
    await runner.stop()


async def test_failing_action_is_logged_and_scenario_continues(caplog):
    class Broken(Recorder):
        def set(self, name, value):
            raise RuntimeError("nope")

    clock = VirtualClock()
    target = Broken()
    runner = ScenarioRunner(
        target, steps({"at": 1, "set": {"a": 1}}, {"at": 2, "release": ["a"]}), clock
    )
    with caplog.at_level(logging.ERROR):
        runner.start()
        await clock.advance(3)
    assert "scenario action failed: set" in caplog.text
    assert target.calls == [("release", "a")]


async def test_stop_cancels_pending_events():
    clock = VirtualClock()
    target = Recorder()
    runner = ScenarioRunner(target, steps({"at": 10, "release": ["a"]}), clock)
    runner.start()
    await runner.stop()
    await clock.advance(20)
    assert target.calls == []


async def test_device_scenario_end_to_end():
    clock = VirtualClock()
    spec = modbus_spec(
        scenario=[
            {"at": "10s", "set": {"setpoint": 120}},
            {"at": "20s", "fault": {"type": "exception", "code": 6}, "duration": "5s"},
            {"at": "30s", "offline": "10s"},
        ]
    )
    async with ModbusDevice(spec, clock=clock) as device:
        await clock.advance(9)
        assert device.get("setpoint") == 75
        await clock.advance(1)
        assert device.get("setpoint") == 120
        await clock.advance(10)
        assert [f.kind for f in device.faults.active] == ["exception"]
        await clock.advance(5)
        assert device.faults.active == []
        await clock.advance(5)
        assert not device.online
        await clock.advance(10)
        assert device.online
        await asyncio.wait_for(device.scenario.wait(), 1)
        assert device.scenario.done
