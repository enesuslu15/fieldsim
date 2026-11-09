"""Fault injection: decides, per request, how a device should misbehave.

The injector is protocol independent. It produces a :class:`FaultPlan` and the
protocol server carries it out (how a "corrupt" response looks is a protocol
detail; *whether* to corrupt is not).
"""

from __future__ import annotations

import random
from collections.abc import Iterable
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from pydantic import TypeAdapter, ValidationError

from fieldsim.config import (
    CorruptFault,
    DisconnectFault,
    DropFault,
    ExceptionFault,
    FaultSpec,
    LatencyFault,
    StatusFault,
)

_FAULT_ADAPTER: TypeAdapter[FaultSpec] = TypeAdapter(FaultSpec)


def parse_fault(fault_type: str, **params: Any) -> FaultSpec:
    """Build a validated fault spec, e.g. ``parse_fault("latency", delay="200ms")``."""
    try:
        return _FAULT_ADAPTER.validate_python({"type": fault_type, **params})
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'][1:]) or 'type'}: {e['msg']}"
            for e in exc.errors(include_url=False)
        )
        raise ValueError(f"invalid {fault_type!r} fault: {details}") from None


@dataclass
class FaultPlan:
    """What to do with one request."""

    delay: float = 0.0
    drop: bool = False
    execute: bool = True
    disconnect: bool = False
    exception: int | None = None
    status: str | int | None = None
    corrupt: str | None = None

    @property
    def clean(self) -> bool:
        return self == FaultPlan()


class ActiveFault:
    """A fault currently attached to a device.

    Usable as a context manager (``with device.fault("drop"): ...``) and keeps
    count of how many requests it affected.
    """

    def __init__(self, injector: FaultInjector, spec: FaultSpec):
        self.spec = spec
        self.hits = 0
        self._injector = injector

    @property
    def kind(self) -> str:
        """Fault type, e.g. ``"latency"``."""
        return self.spec.type

    @property
    def active(self) -> bool:
        return self in self._injector.active

    def remove(self) -> None:
        self._injector.remove(self)

    def __enter__(self) -> ActiveFault:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.remove()

    def __repr__(self) -> str:
        return f"<ActiveFault {self.spec.type} hits={self.hits} active={self.active}>"


class FaultInjector:
    def __init__(self, specs: Iterable[FaultSpec] = (), *, seed: int | None = None):
        self.rng = random.Random(seed)
        self._faults: list[ActiveFault] = []
        for spec in specs:
            self.add(spec)

    @property
    def active(self) -> list[ActiveFault]:
        return list(self._faults)

    def add(self, spec: FaultSpec) -> ActiveFault:
        fault = ActiveFault(self, spec)
        self._faults.append(fault)
        return fault

    def remove(self, fault: ActiveFault) -> None:
        if fault in self._faults:
            self._faults.remove(fault)

    def clear(self) -> None:
        self._faults.clear()

    def plan(self, function_code: int | None = None, service: str | None = None) -> FaultPlan:
        """Roll the dice for one request and combine every fault that applies.

        ``function_code`` (Modbus) and ``service`` (OPC UA) identify the request
        for the faults' filters; ``None`` means "unknown" and matches any filter.
        """
        plan = FaultPlan()
        for fault in list(self._faults):
            spec = fault.spec
            if (
                spec.function_codes is not None
                and function_code is not None
                and function_code not in spec.function_codes
            ):
                continue
            if spec.services is not None and service is not None and service not in spec.services:
                continue
            if spec.probability < 1 and self.rng.random() >= spec.probability:
                continue
            fault.hits += 1
            if spec.count is not None and fault.hits >= spec.count:
                self.remove(fault)
            self._apply(spec, plan)
        return plan

    def _apply(self, spec: FaultSpec, plan: FaultPlan) -> None:
        if isinstance(spec, LatencyFault):
            plan.delay += spec.delay + (self.rng.uniform(0, spec.jitter) if spec.jitter else 0.0)
        elif isinstance(spec, DropFault):
            plan.drop = True
            plan.execute = plan.execute and spec.executed
        elif isinstance(spec, DisconnectFault):
            plan.disconnect = True
        elif isinstance(spec, ExceptionFault):
            plan.exception = spec.code
        elif isinstance(spec, StatusFault):
            plan.status = spec.code
        elif isinstance(spec, CorruptFault):
            plan.corrupt = spec.mode
