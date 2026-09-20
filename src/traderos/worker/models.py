"""Small immutable records used by the unattended paper supervisor."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class WorkerHealth(StrEnum):
    """Operational health; unknown and blocked are never treated as healthy."""

    UNKNOWN = "UNKNOWN"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    HEALTHY = "HEALTHY"


class WorkerState(StrEnum):
    """Process lifecycle state, separate from operational health."""

    UNKNOWN = "UNKNOWN"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


@dataclass(frozen=True)
class DecisionTraceEvent:
    """One durable stage result from a worker cycle."""

    stage: str
    status: str
    reason: str | None = None
    metadata: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe representation for the operational store."""

        result: dict[str, object] = {"stage": self.stage, "status": self.status}
        if self.reason is not None:
            result["reason"] = self.reason
        if self.metadata:
            result["metadata"] = dict(self.metadata)
        return result


@dataclass(frozen=True)
class StrategyHealthMetric:
    """Read-only signal counters derived from durable strategy trace events.

    These counters describe observed signal decisions only.  They are not
    performance measurements and do not grant authority to disable, size, or
    execute a strategy.
    """

    strategy_id: str
    strategy_version: str
    decision_count: int
    long_signal_count: int
    short_signal_count: int
    flat_signal_count: int
    hold_signal_count: int
    intent_count: int
    intent_decision_count: int

    def __post_init__(self) -> None:
        if not self.strategy_id.strip() or not self.strategy_version.strip():
            raise ValueError("strategy health identity must not be blank")
        if any(
            value < 0
            for value in (
                self.decision_count,
                self.long_signal_count,
                self.short_signal_count,
                self.flat_signal_count,
                self.hold_signal_count,
                self.intent_count,
                self.intent_decision_count,
            )
        ):
            raise ValueError("strategy health counters must not be negative")

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe read-only projection."""

        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "decision_count": self.decision_count,
            "long_signal_count": self.long_signal_count,
            "short_signal_count": self.short_signal_count,
            "flat_signal_count": self.flat_signal_count,
            "hold_signal_count": self.hold_signal_count,
            "intent_count": self.intent_count,
            "intent_decision_count": self.intent_decision_count,
        }


DEFAULT_STRATEGY_HEALTH_CYCLE_LIMIT = 1000


@dataclass(frozen=True)
class StrategyHealthReport:
    """Bounded read-only strategy signal report."""

    workload_id: str
    cycle_limit: int
    cycles_considered: int
    truncated: bool
    replayed_cycle_count: int
    structured_decision_count: int
    unattributed_decision_count: int
    metrics: tuple[StrategyHealthMetric, ...]

    def __post_init__(self) -> None:
        if not self.workload_id.strip():
            raise ValueError("strategy health workload must not be blank")
        if self.cycle_limit <= 0 or self.cycles_considered < 0:
            raise ValueError("strategy health report bounds are invalid")
        if self.cycles_considered > self.cycle_limit:
            raise ValueError("strategy health report exceeds its cycle limit")
        if self.replayed_cycle_count < 0 or self.replayed_cycle_count > self.cycles_considered:
            raise ValueError("replayed strategy health cycle count is invalid")
        if self.structured_decision_count < 0:
            raise ValueError("strategy health event count must not be negative")
        if self.unattributed_decision_count < 0:
            raise ValueError("unattributed strategy event count must not be negative")

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe read-only projection."""

        return {
            "workload_id": self.workload_id,
            "cycle_limit": self.cycle_limit,
            "cycles_considered": self.cycles_considered,
            "truncated": self.truncated,
            "replayed_cycle_count": self.replayed_cycle_count,
            "structured_decision_count": self.structured_decision_count,
            "unattributed_decision_count": self.unattributed_decision_count,
            "strategies": [metric.as_dict() for metric in self.metrics],
        }


@dataclass(frozen=True)
class WorkerStatus:
    """Durable status rendered by the status command."""

    workload_id: str
    worker_state: WorkerState
    health: WorkerHealth
    mode: str
    heartbeat_at: datetime | None
    last_cycle_at: datetime | None
    data_status: str
    data_reason: str | None
    data_latest_at: datetime | None
    eligible_strategy_count: int
    active_risk_locks: tuple[str, ...]
    reconciliation_status: str
    last_decision: str
    last_reason_codes: tuple[str, ...]
    paper_order_count: int
    paper_fill_count: int
    stop_requested: bool
    last_error: str | None
    last_trace: tuple[DecisionTraceEvent, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe read-only operational projection."""

        return {
            "workload_id": self.workload_id,
            "worker_state": self.worker_state.value,
            "health": self.health.value,
            "mode": self.mode,
            "heartbeat_at": self.heartbeat_at.isoformat() if self.heartbeat_at else None,
            "last_cycle_at": self.last_cycle_at.isoformat() if self.last_cycle_at else None,
            "data_status": self.data_status,
            "data_reason": self.data_reason,
            "data_latest_at": (
                self.data_latest_at.isoformat() if self.data_latest_at else None
            ),
            "eligible_strategy_count": self.eligible_strategy_count,
            "active_risk_locks": list(self.active_risk_locks),
            "reconciliation_status": self.reconciliation_status,
            "last_decision": self.last_decision,
            "last_reason_codes": list(self.last_reason_codes),
            "paper_order_count": self.paper_order_count,
            "paper_fill_count": self.paper_fill_count,
            "stop_requested": self.stop_requested,
            "last_error": self.last_error,
            "last_trace": [event.as_dict() for event in self.last_trace],
        }

    @classmethod
    def unknown(cls, workload_id: str, mode: str = "PAPER") -> "WorkerStatus":
        return cls(
            workload_id=workload_id,
            worker_state=WorkerState.UNKNOWN,
            health=WorkerHealth.UNKNOWN,
            mode=mode,
            heartbeat_at=None,
            last_cycle_at=None,
            data_status="UNKNOWN",
            data_reason="worker_has_not_started",
            data_latest_at=None,
            eligible_strategy_count=0,
            active_risk_locks=(),
            reconciliation_status="UNKNOWN",
            last_decision="UNKNOWN",
            last_reason_codes=(),
            paper_order_count=0,
            paper_fill_count=0,
            stop_requested=False,
            last_error=None,
        )


@dataclass(frozen=True)
class CycleOutcome:
    """The durable, auditable result of one worker cycle."""

    cycle_id: str
    decision_key: str
    action: str
    health: WorkerHealth
    data_status: str
    data_reason: str | None
    eligible_strategy_count: int
    reconciliation_status: str
    reason_codes: tuple[str, ...]
    trace: tuple[DecisionTraceEvent, ...] = ()
    replayed: bool = False


__all__ = [
    "CycleOutcome",
    "DEFAULT_STRATEGY_HEALTH_CYCLE_LIMIT",
    "DecisionTraceEvent",
    "StrategyHealthMetric",
    "StrategyHealthReport",
    "WorkerHealth",
    "WorkerState",
    "WorkerStatus",
]
