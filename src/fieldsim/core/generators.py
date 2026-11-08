"""Value generators: functions of elapsed time that drive simulated points."""

from __future__ import annotations

import math
import random
from abc import ABC, abstractmethod

from fieldsim.config import (
    ConstantGenerator,
    GeneratorSpec,
    RampGenerator,
    RandomWalkGenerator,
    SequenceGenerator,
    SineGenerator,
)


class Generator(ABC):
    """Produces a value for a point given the seconds elapsed since the device started."""

    @abstractmethod
    def sample(self, t: float) -> float: ...


class Constant(Generator):
    def __init__(self, value: float):
        self.value = value

    def sample(self, t: float) -> float:
        return self.value


class Sine(Generator):
    def __init__(self, minimum: float, maximum: float, period: float, phase: float = 0.0):
        if period <= 0:
            raise ValueError("period must be > 0")
        self.minimum, self.maximum, self.period, self.phase = minimum, maximum, period, phase

    def sample(self, t: float) -> float:
        amplitude = (self.maximum - self.minimum) / 2
        angle = 2 * math.pi * (t + self.phase) / self.period
        return self.minimum + amplitude * (1 + math.sin(angle))


class Ramp(Generator):
    def __init__(self, start: float, end: float, period: float, mode: str = "sawtooth"):
        if period <= 0:
            raise ValueError("period must be > 0")
        if mode not in ("sawtooth", "triangle", "once"):
            raise ValueError(f"unknown ramp mode {mode!r}")
        self.start, self.end, self.period, self.mode = start, end, period, mode

    def sample(self, t: float) -> float:
        if self.mode == "once":
            fraction = min(max(t / self.period, 0.0), 1.0)
        else:
            fraction = (t % self.period) / self.period
            if self.mode == "triangle":
                fraction = 2 * fraction if fraction < 0.5 else 2 - 2 * fraction
        return self.start + (self.end - self.start) * fraction


class RandomWalk(Generator):
    """Stateful: each call to :meth:`sample` takes one step, regardless of ``t``."""

    def __init__(
        self, start: float, step: float, minimum: float, maximum: float, seed: int | None = None
    ):
        self.value = start
        self.step, self.minimum, self.maximum = step, minimum, maximum
        self._rng = random.Random(seed)
        self._started = False

    def sample(self, t: float) -> float:
        if self._started:
            self.value += self._rng.uniform(-self.step, self.step)
            self.value = min(max(self.value, self.minimum), self.maximum)
        self._started = True
        return self.value


class Sequence(Generator):
    def __init__(self, values: list[float], interval: float, repeat: bool = True):
        if not values:
            raise ValueError("values must not be empty")
        if interval <= 0:
            raise ValueError("interval must be > 0")
        self.values, self.interval, self.repeat = list(values), interval, repeat

    def sample(self, t: float) -> float:
        index = max(int(t // self.interval), 0)
        index = index % len(self.values) if self.repeat else min(index, len(self.values) - 1)
        return self.values[index]


def build_generator(spec: GeneratorSpec) -> Generator:
    """Create a generator from its configuration model."""
    if isinstance(spec, ConstantGenerator):
        return Constant(spec.value)
    if isinstance(spec, SineGenerator):
        return Sine(spec.min, spec.max, spec.period, spec.phase)
    if isinstance(spec, RampGenerator):
        return Ramp(spec.start, spec.end, spec.period, spec.mode)
    if isinstance(spec, RandomWalkGenerator):
        return RandomWalk(spec.start, spec.step, spec.min, spec.max, spec.seed)
    if isinstance(spec, SequenceGenerator):
        return Sequence(spec.values, spec.interval, spec.repeat)
    raise TypeError(f"unsupported generator spec: {type(spec).__name__}")  # pragma: no cover
