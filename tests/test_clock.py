import asyncio

import pytest

from fieldsim.core.clock import RealClock, VirtualClock


async def test_real_clock():
    clock = RealClock()
    before = clock.time()
    await clock.sleep(0.01)
    await clock.sleep(-1)  # negative sleeps return immediately
    assert clock.time() > before


async def test_virtual_clock_stands_still():
    clock = VirtualClock(start=5)
    assert clock.time() == 5
    await asyncio.sleep(0.01)
    assert clock.time() == 5


async def test_sleepers_wake_in_deadline_order_at_their_time():
    clock = VirtualClock()
    woke: list[tuple[str, float]] = []

    async def sleeper(name: str, delay: float) -> None:
        await clock.sleep(delay)
        woke.append((name, clock.time()))

    tasks = [asyncio.create_task(sleeper(n, d)) for n, d in [("c", 3), ("a", 1), ("b", 2)]]
    await asyncio.sleep(0)
    assert clock.pending == 3
    await clock.advance(2.5)
    assert woke == [("a", 1), ("b", 2)]
    assert clock.time() == 2.5
    await clock.advance(10)
    assert woke[-1] == ("c", 3)
    assert clock.time() == 12.5
    await asyncio.gather(*tasks)


async def test_periodic_task_runs_every_interval():
    clock = VirtualClock()
    ticks: list[float] = []

    async def periodic() -> None:
        while True:
            await clock.sleep(1)
            ticks.append(clock.time())

    task = asyncio.create_task(periodic())
    await clock.advance(5)
    assert ticks == [1, 2, 3, 4, 5]
    task.cancel()


async def test_cancelled_sleeper_is_skipped():
    clock = VirtualClock()
    task = asyncio.create_task(clock.sleep(1))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert clock.pending == 0
    await clock.advance(2)  # must not blow up on the cancelled future
    assert clock.time() == 2


async def test_zero_sleep_yields():
    clock = VirtualClock()
    await clock.sleep(0)
    assert clock.time() == 0


async def test_cannot_go_backwards():
    with pytest.raises(ValueError, match="backwards"):
        await VirtualClock().advance(-1)
