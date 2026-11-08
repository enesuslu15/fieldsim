import itertools

import pytest

from fieldsim.config import (
    ConstantGenerator,
    RampGenerator,
    RandomWalkGenerator,
    SequenceGenerator,
    SineGenerator,
)
from fieldsim.core.generators import (
    Constant,
    Ramp,
    RandomWalk,
    Sequence,
    Sine,
    build_generator,
)


def test_constant():
    assert Constant(4.2).sample(123) == 4.2


def test_sine_shape():
    gen = Sine(0, 10, period=4)
    assert gen.sample(0) == pytest.approx(5)
    assert gen.sample(1) == pytest.approx(10)
    assert gen.sample(2) == pytest.approx(5)
    assert gen.sample(3) == pytest.approx(0)
    assert gen.sample(4) == pytest.approx(5)


def test_sine_phase():
    assert Sine(0, 10, period=4, phase=1).sample(0) == pytest.approx(10)


@pytest.mark.parametrize(
    ("mode", "samples"),
    [
        ("sawtooth", [(0, 0), (5, 50), (9.99, 99.9), (10, 0), (15, 50)]),
        ("triangle", [(0, 0), (2.5, 50), (5, 100), (7.5, 50), (10, 0)]),
        ("once", [(-1, 0), (0, 0), (5, 50), (10, 100), (50, 100)]),
    ],
)
def test_ramp(mode, samples):
    gen = Ramp(0, 100, period=10, mode=mode)
    for t, expected in samples:
        assert gen.sample(t) == pytest.approx(expected), t


def test_random_walk_is_bounded_and_reproducible():
    a = RandomWalk(start=5, step=2, minimum=0, maximum=10, seed=1)
    b = RandomWalk(start=5, step=2, minimum=0, maximum=10, seed=1)
    seq_a = [a.sample(t) for t in range(200)]
    seq_b = [b.sample(t) for t in range(200)]
    assert seq_a == seq_b
    assert seq_a[0] == 5  # first sample is the start value
    assert all(0 <= v <= 10 for v in seq_a)
    assert all(abs(x - y) <= 2 for x, y in itertools.pairwise(seq_a))
    assert len(set(seq_a)) > 10


def test_sequence_repeat_and_hold():
    rep = Sequence([1, 2, 3], interval=2)
    assert [rep.sample(t) for t in (0, 1.9, 2, 4, 6, 7)] == [1, 1, 2, 3, 1, 1]
    hold = Sequence([1, 2, 3], interval=2, repeat=False)
    assert [hold.sample(t) for t in (0, 4, 100)] == [1, 3, 3]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Sine(0, 1, period=0),
        lambda: Ramp(0, 1, period=0),
        lambda: Ramp(0, 1, period=1, mode="zigzag"),
        lambda: Sequence([], interval=1),
        lambda: Sequence([1], interval=0),
    ],
)
def test_invalid_arguments(factory):
    with pytest.raises(ValueError):  # noqa: PT011
        factory()


@pytest.mark.parametrize(
    ("spec", "cls"),
    [
        (ConstantGenerator(type="constant", value=1), Constant),
        (SineGenerator(type="sine", min=0, max=1, period=1), Sine),
        (RampGenerator(type="ramp", start=0, end=1, period=1), Ramp),
        (RandomWalkGenerator(type="random_walk", start=0, step=1, min=0, max=1), RandomWalk),
        (SequenceGenerator(type="sequence", values=[1], interval=1), Sequence),
    ],
)
def test_build_generator(spec, cls):
    assert isinstance(build_generator(spec), cls)
