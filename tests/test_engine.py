import asyncio
import logging

import pytest

from fieldsim.core.engine import GeneratorEngine
from fieldsim.core.generators import Generator, Ramp


class DictStore:
    def __init__(self):
        self.values = {}

    def get(self, name):
        return self.values[name]

    def set(self, name, value, *, clamp=False):
        self.values[name] = value

    def names(self):
        return list(self.values)


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_tick_and_pinning():
    store = DictStore()
    engine = GeneratorEngine(store, {"x": Ramp(0, 10, period=10)}, interval=1)
    engine.tick(5)
    assert store.values["x"] == 5
    engine.pin("x")
    engine.pin("not-generated")  # ignored
    assert engine.pinned == {"x"}
    engine.tick(7)
    assert store.values["x"] == 5
    engine.release("x")
    engine.tick(7)
    assert store.values["x"] == 7


def test_interval_must_be_positive():
    with pytest.raises(ValueError, match="interval"):
        GeneratorEngine(DictStore(), {}, interval=0)


async def test_start_samples_immediately_then_periodically():
    store = DictStore()
    clock = FakeClock()
    engine = GeneratorEngine(
        store, {"x": Ramp(0, 100, period=100, mode="once")}, interval=0.01, clock=clock
    )
    engine.start()
    assert engine.running
    assert store.values["x"] == 0
    clock.now += 42
    await asyncio.sleep(0.05)
    assert store.values["x"] == 42
    engine.start()  # idempotent
    await engine.stop()
    assert not engine.running
    await engine.stop()  # idempotent


async def test_engine_without_generators_does_not_start():
    engine = GeneratorEngine(DictStore(), {}, interval=0.01)
    engine.start()
    assert not engine.running


async def test_failing_generator_is_logged_and_engine_survives(caplog):
    class Flaky(Generator):
        calls = 0

        def sample(self, t):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("boom")
            return self.calls

    store = DictStore()
    gen = Flaky()
    engine = GeneratorEngine(store, {"x": gen}, interval=0.005)
    with caplog.at_level(logging.ERROR):
        engine.start()
        await asyncio.sleep(0.05)
        await engine.stop()
    assert "generator tick failed" in caplog.text
    assert gen.calls > 3
