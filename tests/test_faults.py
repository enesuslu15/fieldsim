import pytest

from fieldsim.config import DropFault, LatencyFault
from fieldsim.core.faults import FaultInjector, FaultPlan, parse_fault


def test_parse_fault():
    spec = parse_fault("latency", delay="250ms", jitter=0.1)
    assert isinstance(spec, LatencyFault)
    assert spec.delay == 0.25


@pytest.mark.parametrize(
    ("kind", "params", "message"),
    [
        ("latency", {}, "delay: Field required"),
        ("drop", {"probability": 2}, "probability"),
        ("teleport", {}, "type"),
        ("corrupt", {"mode": "sparkles"}, "mode"),
    ],
)
def test_parse_fault_errors(kind, params, message):
    with pytest.raises(ValueError, match=f"invalid '{kind}' fault") as info:
        parse_fault(kind, **params)
    assert message in str(info.value)


def test_no_faults_gives_clean_plan():
    plan = FaultInjector().plan(3)
    assert plan.clean
    assert plan == FaultPlan()


def test_plans_combine():
    injector = FaultInjector(
        [
            parse_fault("latency", delay=0.1),
            parse_fault("latency", delay=0.2),
            parse_fault("exception", code=4),
            parse_fault("corrupt", mode="garbage"),
        ]
    )
    plan = injector.plan(3)
    assert plan.delay == pytest.approx(0.3)
    assert plan.exception == 4
    assert plan.corrupt == "garbage"
    assert not plan.clean


def test_jitter_is_bounded_and_seeded():
    def delays(seed):
        injector = FaultInjector([parse_fault("latency", delay=1, jitter=0.5)], seed=seed)
        return [injector.plan().delay for _ in range(50)]

    first = delays(7)
    assert first == delays(7)
    assert all(1 <= d <= 1.5 for d in first)
    assert len(set(first)) > 1


def test_drop_executed_flag():
    plan = FaultInjector([parse_fault("drop")]).plan()
    assert plan.drop
    assert not plan.execute
    plan = FaultInjector([parse_fault("drop", executed=True)]).plan()
    assert plan.drop
    assert plan.execute


def test_disconnect():
    assert FaultInjector([parse_fault("disconnect")]).plan().disconnect


def test_probability():
    never = FaultInjector([parse_fault("drop", probability=0)])
    assert not any(never.plan().drop for _ in range(100))
    half = FaultInjector([parse_fault("drop", probability=0.5)], seed=1)
    hits = sum(half.plan().drop for _ in range(1000))
    assert 400 < hits < 600


def test_function_code_filter():
    injector = FaultInjector([parse_fault("drop", function_codes=[6, 16])])
    assert not injector.plan(3).drop
    assert injector.plan(6).drop
    assert injector.plan(None).drop  # unknown function code: filter does not apply


def test_count_removes_the_fault():
    injector = FaultInjector()
    fault = injector.add(DropFault(type="drop", count=2))
    assert [injector.plan().drop for _ in range(3)] == [True, True, False]
    assert fault.hits == 2
    assert not fault.active


def test_active_fault_context_manager_and_clear():
    injector = FaultInjector()
    with injector.add(parse_fault("disconnect")) as fault:
        assert fault.active
        assert fault.kind == "disconnect"
        assert "active=True" in repr(fault)
    assert not fault.active
    fault.remove()  # removing twice is harmless
    injector.add(parse_fault("drop"))
    injector.clear()
    assert injector.active == []


def test_service_filter_and_status():
    injector = FaultInjector([parse_fault("status", code="BadTimeout", services=["Read"])])
    assert injector.plan(service="Write").clean
    assert injector.plan(service="Read").status == "BadTimeout"
    assert injector.plan(service=None).status == "BadTimeout"  # unknown service
