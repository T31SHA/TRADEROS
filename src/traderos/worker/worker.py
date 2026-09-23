"""Fail-closed unattended PAPER-mode supervisory loop.

The worker composes the existing governance, data, strategy, feature, regime,
fusion, risk, sizing, paper execution, and accounting boundaries. It does not
create a parallel strategy, risk, sizing, execution, or accounting engine.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from traderos.core.config import Settings, TradingMode
from traderos.data.errors import DataValidationError
from traderos.data.hashing import market_bar_content_hash
from traderos.data.lineage import AdjustmentPolicy
from traderos.data.quality import QualitySeverity
from traderos.data.storage import MarketDataStore
from traderos.data.time import require_utc
from traderos.data.timeframes import Timeframe
from traderos.database.connection import create_database_engine
from traderos.database.store import SqlAlchemyMarketDataStore
from traderos.execution.authority import ExecutionAuthority
from traderos.monitoring import HealthCheck, LocalSystemHealthMonitor
from traderos.paper.engine import OperationalPaperTradingEngine, PaperTradingEngine
from traderos.paper.models import PaperTradingError
from traderos.paper.store import SqlAlchemyPaperStore
from traderos.regimes import EmaPercentileRegimeDetector
from traderos.research.dataset import DatasetQualificationStatus, ResearchDatasetEligibility
from traderos.research.dataset_records import (
    DatasetQualificationRegistry,
    DatasetRecordStatus,
)
from traderos.research.strategy_lifecycle import (
    ActorCapability,
    ActorDirectory,
    LifecycleError,
)
from traderos.research.strategy_service import StrategyGovernanceService
from traderos.research.strategy_store import StrategyGovernanceStore
from traderos.risk import RiskFirewall, SystemHealthSnapshot
from traderos.signals import MajorityVoteFusionPolicy
from traderos.strategies import (
    EquityBreakoutStrategy,
    EquityMomentumStrategy,
    ForexBreakoutStrategy,
    ForexTrendFollowingStrategy,
)
from traderos.strategies.base import Strategy
from traderos.strategies.runtime import implementation_locator, runtime_content_identity
from traderos.worker.models import (
    DEFAULT_STRATEGY_HEALTH_CYCLE_LIMIT,
    CycleOutcome,
    DecisionTraceEvent,
    StrategyHealthReport,
    WorkerHealth,
    WorkerState,
    WorkerStatus,
)
from traderos.worker.pipeline import (
    PaperDecisionPipeline,
    StrategyRuntimeBinding,
)
from traderos.worker.store import WorkerStore


class WorkerError(RuntimeError):
    """Base error for fail-closed worker control operations."""


class WorkerConfigurationError(WorkerError):
    """Raised when configuration cannot be used by the PAPER-only worker."""


class WorkerBusy(WorkerError):
    """Raised when another owner holds the configured workload lease."""


@dataclass(frozen=True)
class DataAdmission:
    status: str
    reason: str | None
    latest_timestamp: datetime | None
    dataset_version: str | None = None
    dataset_hash: str | None = None
    adjustment_policy: AdjustmentPolicy | None = None


@dataclass(frozen=True)
class WorkerDependencies:
    market_store: MarketDataStore
    paper_engine: PaperTradingEngine
    governance: StrategyGovernanceService | None
    pipeline: PaperDecisionPipeline | None = None
    dataset_qualification_registry: DatasetQualificationRegistry | None = None
    system_health_provider: (
        Callable[[datetime, Sequence[HealthCheck]], SystemHealthSnapshot] | None
    ) = None


def _default_runtime_bindings() -> tuple[StrategyRuntimeBinding, ...]:
    strategies = tuple(
        cast(Strategy, strategy)
        for strategy in (
            ForexTrendFollowingStrategy(),
            ForexBreakoutStrategy(),
            EquityMomentumStrategy(),
            EquityBreakoutStrategy(),
        )
    )
    return tuple(
        StrategyRuntimeBinding(
            implementation_id=strategy.strategy_id,
            implementation_version=strategy.strategy_version,
            code_identity=runtime_content_identity(strategy),
            strategy=strategy,
            implementation_locator=implementation_locator(strategy),
        )
        for strategy in strategies
    )


def _parse_strategy_versions(value: str) -> tuple[tuple[str, str], ...]:
    parsed: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            continue
        separator = "@" if "@" in item else ":" if ":" in item else ""
        if not separator:
            raise WorkerConfigurationError(
                "WORKER_STRATEGY_VERSIONS must use strategy_id@version entries"
            )
        strategy_id, version = item.rsplit(separator, 1)
        if not strategy_id.strip() or not version.strip():
            raise WorkerConfigurationError("configured strategy versions must not be blank")
        identity = (strategy_id.strip(), version.strip())
        if identity in seen:
            raise WorkerConfigurationError(
                "WORKER_STRATEGY_VERSIONS must not contain duplicate strategy identities"
            )
        seen.add(identity)
        parsed.append(identity)
    return tuple(parsed)


class PaperWorker:
    """One restartable, single-workload PAPER supervisor."""

    def __init__(
        self,
        settings: Settings,
        *,
        store: WorkerStore,
        dependencies: WorkerDependencies,
        clock: Callable[[], datetime] | None = None,
        owner_id: str | None = None,
    ) -> None:
        if settings.trading_mode is not TradingMode.PAPER or settings.live_execution_permitted:
            raise WorkerConfigurationError(
                "the autonomous worker is PAPER-only; live mode is rejected"
            )
        if settings.worker_data_source.strip() == "":
            raise WorkerConfigurationError("worker data source must not be blank")
        self.settings = settings
        self.store = store
        self.dependencies = dependencies
        self._clock = clock or (lambda: datetime.now(UTC))
        self.owner_id = owner_id or f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex}"
        self._stop_event = threading.Event()
        self._lease_held = False
        self._execution_authority: ExecutionAuthority | None = None
        self._closed = False
        self._strategies = _parse_strategy_versions(settings.worker_strategy_versions)

    @classmethod
    def from_settings(cls, settings: Settings) -> PaperWorker:
        """Build the local, database-backed worker without network adapters."""

        engine = create_database_engine(settings.database_url)
        try:
            worker_store = WorkerStore(engine)
            try:
                worker_store.create_schema()
            except (SQLAlchemyError, ValueError) as exc:
                raise WorkerConfigurationError(
                    "worker persistence schema is unavailable"
                ) from exc
            market_store = SqlAlchemyMarketDataStore(engine)
            paper_store = SqlAlchemyPaperStore(engine)
            governance_store = StrategyGovernanceStore(engine)
            runtime_bindings = _default_runtime_bindings()
            system_health_monitor = LocalSystemHealthMonitor()
            dataset_qualification_registry = (
                DatasetQualificationRegistry(Path(settings.worker_dataset_qualification_root))
                if settings.worker_dataset_qualification_root
                else None
            )
            governance = StrategyGovernanceService(
                governance_store,
                ActorDirectory({"worker-read": (ActorCapability.PROVENANCE_READ,)}),
                registered_implementations=tuple(
                    (item.implementation_id, item.implementation_version)
                    for item in runtime_bindings
                ),
            )
            paper_engine = OperationalPaperTradingEngine(paper_store)
            return cls(
                settings,
                store=worker_store,
                dependencies=WorkerDependencies(
                    market_store=market_store,
                    paper_engine=paper_engine,
                    governance=governance,
                    pipeline=PaperDecisionPipeline(
                        paper_engine=paper_engine,
                        governance=governance,
                        runtime_bindings=runtime_bindings,
                        regime_detector=EmaPercentileRegimeDetector(),
                        fusion_policy=MajorityVoteFusionPolicy(),
                        risk_firewall=RiskFirewall(),
                    ),
                    dataset_qualification_registry=dataset_qualification_registry,
                    system_health_provider=system_health_monitor.snapshot,
                ),
            )
        except Exception:
            engine.dispose()
            raise

    def request_shutdown(self) -> None:
        """Request a graceful local shutdown after the current safe boundary."""

        self._stop_event.set()

    def close(self) -> None:
        """Release the worker's database resources; safe to call repeatedly."""

        if self._closed:
            return
        self.store.engine.dispose()
        self._closed = True

    def run_once(self) -> WorkerStatus:
        """Run exactly one bounded cycle, then release the workload lease."""

        self._begin(reset_stop=False)
        try:
            self._run_cycle()
        except Exception as exc:
            self._mark_failure(exc)
            raise
        finally:
            self._finish()
        return self.store.observed_status(
            self.settings.worker_workload_id,
            now=self._now(),
            lease_key=self._lease_key(),
        )

    def run_forever(self, *, max_cycles: int | None = None) -> WorkerStatus:
        """Run until stopped, an external stop request is observed, or bounded."""

        if max_cycles is not None and max_cycles <= 0:
            raise WorkerConfigurationError("max_cycles must be positive")
        self._begin(reset_stop=True)
        cycles = 0
        try:
            while not self._stop_event.is_set():
                self._run_cycle()
                cycles += 1
                if max_cycles is not None and cycles >= max_cycles:
                    break
                if self.store.status(self.settings.worker_workload_id).stop_requested:
                    break
                self._wait_for_next_cycle()
                if self.store.status(self.settings.worker_workload_id).stop_requested:
                    break
        except KeyboardInterrupt:
            self.request_shutdown()
        except Exception as exc:
            self._mark_failure(exc)
            raise
        finally:
            self._finish()
        return self.store.observed_status(
            self.settings.worker_workload_id,
            now=self._now(),
            lease_key=self._lease_key(),
        )

    def _begin(self, *, reset_stop: bool) -> None:
        now = self._now()
        workload_id = self.settings.worker_workload_id
        try:
            self.store.create_schema()
        except (SQLAlchemyError, ValueError) as exc:
            raise WorkerError("worker persistence schema is unavailable") from exc
        try:
            acquired = self.store.acquire_lease(
                workload_id,
                self.owner_id,
                now=now,
                duration=timedelta(seconds=self.settings.worker_lease_seconds),
                lease_key=self._lease_key(),
            )
        except SQLAlchemyError as exc:
            raise WorkerError("worker lease persistence is unavailable") from exc
        if not acquired:
            raise WorkerBusy(
                f"account/workload {workload_id!r} already has an active worker"
            )
        self._lease_held = True
        self._execution_authority = acquired
        try:
            current = self.store.status(workload_id)
            started = replace(
                current,
                worker_state=WorkerState.STARTING,
                health=WorkerHealth.UNKNOWN,
                mode="PAPER",
                heartbeat_at=now,
                stop_requested=False if reset_stop else current.stop_requested,
                last_error=None,
            )
            if not self._save_owned_status(
                started, updated_at=now, preserve_stop_request=not reset_stop
            ):
                raise WorkerError("worker lease was lost while starting")
        except SQLAlchemyError as exc:
            self._release_after_start_failure()
            raise WorkerError("worker lease persistence is unavailable") from exc
        except Exception:
            self._release_after_start_failure()
            raise

    def _release_after_start_failure(self) -> None:
        """Release a lease acquired by a startup path that could not initialize status."""

        if not self._lease_held:
            return
        try:
            self.store.release_lease(
                self.settings.worker_workload_id,
                self.owner_id,
                lease_key=self._lease_key(),
            )
        except Exception:
            # The original startup failure remains authoritative; an expired
            # lease is safer than reporting a usable worker after cleanup fails.
            pass
        finally:
            self._lease_held = False
            self._execution_authority = None

    def _finish(self) -> None:
        if not self._lease_held:
            return
        now = self._now()
        workload_id = self.settings.worker_workload_id
        current = self.store.status(workload_id)
        self._save_owned_status(
            replace(current, worker_state=WorkerState.STOPPED, heartbeat_at=now),
            updated_at=now,
        )
        self.store.release_lease(workload_id, self.owner_id, lease_key=self._lease_key())
        self._lease_held = False
        self._execution_authority = None

    def _mark_failure(self, error: Exception) -> None:
        """Persist supervisor failures before releasing its workload lease."""

        if not self._lease_held:
            return
        now = self._now()
        current = self.store.status(self.settings.worker_workload_id)
        self._save_owned_status(
            replace(
                current,
                worker_state=WorkerState.STOPPING,
                health=WorkerHealth.FAILED,
                last_error=str(error) or error.__class__.__name__,
                heartbeat_at=now,
            ),
            updated_at=now,
        )

    def _run_cycle(self) -> CycleOutcome:
        started = self._now()
        workload_id = self.settings.worker_workload_id
        current = self.store.status(workload_id)
        reasons: list[str] = []
        data = DataAdmission("UNKNOWN", "DATA_NOT_EVALUATED", None)
        eligible_count = 0
        strategy_reasons: tuple[str, ...] = ()
        reconciliation_status = current.reconciliation_status
        active_locks: tuple[str, ...] = current.active_risk_locks
        decision_locks = active_locks
        decision_health_status = "NOT_EVALUATED"
        order_count = current.paper_order_count
        fill_count = current.paper_fill_count
        last_error: str | None = None
        health = WorkerHealth.BLOCKED
        action = "NO_TRADE"
        execution_health_determined = False
        trace: list[DecisionTraceEvent] = []
        gate_state = (
            "STOP_REQUESTED"
            if current.stop_requested
            else "KILL_SWITCH_ACTIVE"
            if self.settings.kill_switch_active
            else "OPEN"
        )

        try:
            if current.stop_requested:
                reasons.append("STOP_REQUESTED")
                trace.append(DecisionTraceEvent("global_gate", "BLOCKED", "STOP_REQUESTED"))
            elif self.settings.kill_switch_active:
                reasons.append("KILL_SWITCH_ACTIVE")
                trace.append(DecisionTraceEvent("global_gate", "BLOCKED", "KILL_SWITCH_ACTIVE"))
            else:
                try:
                    self.store.health_check()
                except Exception:
                    reasons.append("PERSISTENCE_HEALTH_CHECK_FAILED")
                    trace.append(
                        DecisionTraceEvent(
                            "persistence", "FAILED", "PERSISTENCE_HEALTH_CHECK_FAILED"
                        )
                    )
                    raise
                trace.append(DecisionTraceEvent("persistence", "HEALTHY"))
                try:
                    reconciliation_status, active_locks, order_count, fill_count = self._reconcile()
                    decision_locks = active_locks
                except Exception:
                    reasons.append("RECONCILIATION_FAILED")
                    reconciliation_status = "FAILED"
                    trace.append(
                        DecisionTraceEvent("reconciliation", "FAILED", "RECONCILIATION_FAILED")
                    )
                    raise
                if reconciliation_status != "HEALTHY":
                    reconciliation_reason = (
                        "RECONCILIATION_FAILED"
                        if reconciliation_status == "FAILED"
                        else "ACCOUNT_NOT_CONFIGURED"
                    )
                    reasons.append(reconciliation_reason)
                    trace.append(
                        DecisionTraceEvent(
                            "reconciliation", reconciliation_status, reconciliation_reason
                        )
                    )
                else:
                    trace.append(DecisionTraceEvent("reconciliation", "HEALTHY"))
                data = self._admit_data(started)
                if data.reason is not None:
                    reasons.append(data.reason)
                trace.append(
                    DecisionTraceEvent(
                        "data_admission", data.status, data.reason or "DATA_ADMITTED"
                    )
                )
                eligible_count, strategy_reasons = self._resolve_strategy_eligibility()
                reasons.extend(strategy_reasons)
                trace.append(
                    DecisionTraceEvent(
                        "strategy_eligibility",
                        "HEALTHY" if eligible_count > 0 else "BLOCKED",
                        f"ELIGIBLE_COUNT={eligible_count}"
                        if eligible_count > 0
                        else (strategy_reasons[0] if strategy_reasons else "NO_ELIGIBLE_STRATEGY"),
                    )
                )
                if (
                    reconciliation_status == "HEALTHY"
                    and data.status == "HEALTHY"
                ):
                    pipeline = self.dependencies.pipeline
                    if pipeline is None:
                        reasons.append("EXECUTION_PATH_UNSUPPORTED")
                        trace.append(
                            DecisionTraceEvent(
                                "execution", "BLOCKED", "EXECUTION_PATH_UNSUPPORTED"
                            )
                        )
                    else:
                        account_id = self.settings.worker_account_id
                        latest = self.dependencies.market_store.latest_bar(
                            self.settings.worker_instrument or "",
                            Timeframe(self.settings.worker_timeframe or ""),
                            source=self.settings.worker_data_source,
                            adjustment_policy=data.adjustment_policy,
                        )
                        dataset_version = data.dataset_version
                        if account_id is None or latest is None or dataset_version is None:
                            reasons.append("PAPER_PIPELINE_INPUT_UNAVAILABLE")
                            execution_health_determined = True
                            trace.append(
                                DecisionTraceEvent(
                                    "execution", "BLOCKED", "PAPER_PIPELINE_INPUT_UNAVAILABLE"
                                )
                            )
                        else:
                            if data.latest_timestamp != latest.timestamp:
                                data = DataAdmission(
                                    "BLOCKED",
                                    "DATA_CHANGED_DURING_CYCLE",
                                    latest.timestamp,
                                    data.dataset_version,
                                    data.dataset_hash,
                                    data.adjustment_policy,
                                )
                                reasons.append("DATA_CHANGED_DURING_CYCLE")
                                trace.append(
                                    DecisionTraceEvent(
                                        "execution", "BLOCKED", "DATA_CHANGED_DURING_CYCLE"
                                    )
                                )
                                execution_health_determined = True
                            else:
                                recheck_now = self._now()
                                rechecked_data = self._admit_data(recheck_now)
                                if (
                                    rechecked_data.status != "HEALTHY"
                                    or rechecked_data.latest_timestamp != data.latest_timestamp
                                    or rechecked_data.dataset_version != data.dataset_version
                                    or rechecked_data.dataset_hash != data.dataset_hash
                                    or rechecked_data.adjustment_policy != data.adjustment_policy
                                ):
                                    data = DataAdmission(
                                        "BLOCKED",
                                        "DATA_CHANGED_DURING_CYCLE",
                                        latest.timestamp,
                                        data.dataset_version,
                                        data.dataset_hash,
                                        data.adjustment_policy,
                                    )
                                    reasons.append("DATA_CHANGED_DURING_CYCLE")
                                    trace.append(
                                        DecisionTraceEvent(
                                            "execution", "BLOCKED", "DATA_CHANGED_DURING_CYCLE"
                                        )
                                    )
                                    execution_health_determined = True
                            if not execution_health_determined:
                                processing_timestamp = recheck_now
                                system_health = (
                                    self.dependencies.system_health_provider(
                                        processing_timestamp,
                                        (
                                            HealthCheck("worker_lease", self._lease_held),
                                            HealthCheck("persistence", True),
                                            HealthCheck(
                                                "reconciliation", reconciliation_status == "HEALTHY"
                                            ),
                                            HealthCheck(
                                                "data_admission", data.status == "HEALTHY"
                                            ),
                                            HealthCheck(
                                                "execution_pipeline", pipeline is not None
                                            ),
                                        ),
                                    )
                                    if self.dependencies.system_health_provider is not None
                                    else None
                                )
                                decision_health_status = (
                                    system_health.status.value
                                    if system_health is not None
                                    else "UNAVAILABLE"
                                )
                                decision_key = self._decision_key(
                                    data=data,
                                    eligible_count=eligible_count,
                                    reconciliation_status=reconciliation_status,
                                    active_locks=decision_locks,
                                    system_health_status=decision_health_status,
                                    strategy_reasons=strategy_reasons,
                                    gate_state=gate_state,
                                )
                                prior_health = self.store.decision_health(
                                    workload_id, decision_key
                                )
                            if (
                                not execution_health_determined
                                and prior_health is not None
                                and prior_health is not WorkerHealth.FAILED
                            ):
                                reasons.append("DUPLICATE_DECISION")
                                trace.append(
                                    DecisionTraceEvent(
                                        "idempotency", "REPLAYED", "DUPLICATE_DECISION"
                                    )
                                )
                                health = prior_health
                                execution_health_determined = True
                            elif not execution_health_determined:
                                execution_now = self._renew_lease_before_execution()
                                bars = self.dependencies.market_store.query_bars(
                                    latest.symbol,
                                    latest.timeframe,
                                    latest.timestamp - latest.timeframe.duration * 1000,
                                    latest.timestamp + latest.timeframe.duration,
                                    source=self.settings.worker_data_source,
                                    adjustment_policy=data.adjustment_policy,
                                )
                                pipeline_outcome = pipeline.run(
                                    account_id=account_id,
                                    strategy_keys=self._strategies,
                                    bars=bars,
                                    dataset_version=dataset_version,
                                    dataset_hash=data.dataset_hash,
                                    now=execution_now,
                                    execution_authority=self._execution_authority,
                                    processing_clock=self._now,
                                    system_health=system_health,
                                    quality_events=self.dependencies.market_store.quality_events(),
                                )
                                self._require_owned_lease_after_execution()
                                action = pipeline_outcome.action
                                reasons.extend(pipeline_outcome.reason_codes)
                                trace.extend(
                                    DecisionTraceEvent(
                                        event.stage,
                                        event.status,
                                        event.reason,
                                        event.metadata,
                                    )
                                    for event in pipeline_outcome.trace
                                )
                                health = {
                                    "HEALTHY": WorkerHealth.HEALTHY,
                                    "FAILED": WorkerHealth.FAILED,
                                }.get(pipeline_outcome.health.upper(), WorkerHealth.BLOCKED)
                                execution_health_determined = True
                            paper_store = self.dependencies.paper_engine.store
                            active_locks = tuple(
                                sorted(lock.value for lock in paper_store.active_locks(account_id))
                            )
                            order_count = len(paper_store.orders(account_id))
                            fill_count = len(paper_store.fills(account_id))
                else:
                    trace.append(
                        DecisionTraceEvent("execution", "BLOCKED", "PREREQUISITES_NOT_MET")
                    )
                if not execution_health_determined:
                    if reconciliation_status == "FAILED":
                        health = WorkerHealth.FAILED
                    else:
                        health = (
                            WorkerHealth.HEALTHY
                            if data.status == "HEALTHY"
                            and eligible_count > 0
                            and reconciliation_status == "HEALTHY"
                            and "EXECUTION_PATH_UNSUPPORTED" not in reasons
                            else WorkerHealth.BLOCKED
                        )
        except Exception as exc:
            health = WorkerHealth.FAILED
            reasons.append("WORKER_CYCLE_FAILED")
            if reconciliation_status == "UNKNOWN" and self.settings.worker_account_id:
                reconciliation_status = "FAILED"
            last_error = str(exc)
            trace.append(DecisionTraceEvent("worker_cycle", "FAILED", "WORKER_CYCLE_FAILED"))

        if current.stop_requested or self.settings.kill_switch_active:
            traced_stages = {event.stage for event in trace}
            trace.extend(
                DecisionTraceEvent(stage, "UNKNOWN", "NOT_REACHED")
                for stage in (
                    "persistence",
                    "reconciliation",
                    "data_admission",
                    "strategy_eligibility",
                    "execution",
                )
                if stage not in traced_stages
            )

        completed = self._now()
        decision_key = self._decision_key(
            data=data,
            eligible_count=eligible_count,
            reconciliation_status=reconciliation_status,
            active_locks=decision_locks,
            system_health_status=decision_health_status,
            strategy_reasons=strategy_reasons,
            gate_state=gate_state,
        )
        reason_codes = tuple(dict.fromkeys(reasons)) or ("NO_ACTIONABLE_SIGNAL",)
        trace.append(DecisionTraceEvent("decision", action, reason_codes[0]))
        outcome = CycleOutcome(
            cycle_id=f"cycle:{uuid4().hex}",
            decision_key=decision_key,
            action=action,
            health=health,
            data_status=data.status,
            data_reason=data.reason,
            eligible_strategy_count=eligible_count,
            reconciliation_status=reconciliation_status,
            reason_codes=reason_codes,
            trace=tuple(trace),
        )
        persisted = self.store.record_cycle_if_lease_owned(
            workload_id=workload_id,
            decision_key=decision_key,
            started_at=started,
            completed_at=completed,
            outcome=outcome,
            owner_id=self.owner_id,
            lease_key=self._lease_key(),
        )
        if persisted is None:
            # A replacement worker owns the workload.  Do not append a stale
            # result that could make the replacement replay an unexecuted
            # decision.
            self._lease_held = False
            self._stop_event.set()
            return outcome
        if self._lease_held:
            state = self.store.status(workload_id)
            self._save_owned_status(
                replace(
                    state,
                    worker_state=WorkerState.RUNNING,
                    health=persisted.health,
                    mode="PAPER",
                    heartbeat_at=completed,
                    last_cycle_at=completed,
                    data_status=persisted.data_status,
                    data_reason=persisted.data_reason,
                    data_latest_at=data.latest_timestamp,
                    eligible_strategy_count=persisted.eligible_strategy_count,
                    active_risk_locks=active_locks,
                    reconciliation_status=persisted.reconciliation_status,
                    last_decision=persisted.action,
                    last_reason_codes=persisted.reason_codes,
                    paper_order_count=order_count,
                    paper_fill_count=fill_count,
                    last_error=last_error,
                    last_trace=persisted.trace,
                ),
                updated_at=completed,
            )
        return persisted

    def _reconcile(self) -> tuple[str, tuple[str, ...], int, int]:
        account_id = self.settings.worker_account_id
        if account_id is None or not account_id.strip():
            return "UNKNOWN", (), 0, 0
        self.dependencies.paper_engine.reconcile(account_id)
        paper_store = self.dependencies.paper_engine.store
        return (
            "HEALTHY",
            tuple(sorted(lock.value for lock in paper_store.active_locks(account_id))),
            len(paper_store.orders(account_id)),
            len(paper_store.fills(account_id)),
        )

    def _admit_data(self, now: datetime) -> DataAdmission:
        symbol = self.settings.worker_instrument
        timeframe_value = self.settings.worker_timeframe
        if not symbol or not timeframe_value:
            return DataAdmission("BLOCKED", "DATA_NOT_CONFIGURED", None)
        if not self.settings.worker_dataset_version:
            return DataAdmission("BLOCKED", "DATA_DATASET_NOT_CONFIGURED", None)
        if any(
            self._looks_like_fixture_label(value)
            for value in (
                self.settings.worker_data_source,
                self.settings.worker_dataset_version,
            )
        ):
            return DataAdmission("BLOCKED", "DATA_FIXTURE_NOT_OPERATIONAL", None)
        if self.settings.market_data_provider != "local":
            return DataAdmission("BLOCKED", "DATA_PROVIDER_NOT_SUPPORTED", None)
        try:
            timeframe = Timeframe(timeframe_value)
        except ValueError:
            return DataAdmission("BLOCKED", "TIMEFRAME_INVALID", None)
        latest = self.dependencies.market_store.latest_bar(
            symbol,
            timeframe,
            source=self.settings.worker_data_source,
        )
        if latest is None:
            return DataAdmission("BLOCKED", "DATA_UNAVAILABLE", None)
        decision_timestamp = latest.timestamp + timeframe.duration
        if latest.timestamp > now or latest.ingestion_timestamp > decision_timestamp or (
            latest.quote_timestamp is not None and latest.quote_timestamp > now
        ):
            return DataAdmission("BLOCKED", "DATA_FUTURE_DATED", latest.timestamp)
        if latest.timestamp + timeframe.duration > now:
            return DataAdmission("BLOCKED", "DATA_INCOMPLETE", latest.timestamp)
        if now - latest.timestamp > timedelta(seconds=self.settings.worker_data_max_age_seconds):
            return DataAdmission("BLOCKED", "DATA_STALE", latest.timestamp)
        if now - decision_timestamp > self.dependencies.paper_engine.config.decision_max_age:
            return DataAdmission("BLOCKED", "DATA_DECISION_STALE", latest.timestamp)
        if latest.bid is None or latest.ask is None:
            return DataAdmission("BLOCKED", "DATA_QUOTE_UNAVAILABLE", latest.timestamp)
        if latest.quote_timestamp is None:
            return DataAdmission(
                "BLOCKED", "DATA_QUOTE_TIMESTAMP_UNAVAILABLE", latest.timestamp
            )
        if latest.quote_timestamp > decision_timestamp:
            return DataAdmission("BLOCKED", "DATA_QUOTE_FUTURE_DATED", latest.timestamp)
        if (
            now - latest.quote_timestamp
            > self.dependencies.paper_engine.config.quote_max_age
        ):
            return DataAdmission("BLOCKED", "DATA_QUOTE_STALE", latest.timestamp)
        try:
            bars = self.dependencies.market_store.query_bars(
                symbol,
                timeframe,
                latest.timestamp - timeframe.duration * 100,
                decision_timestamp,
                source=self.settings.worker_data_source,
                adjustment_policy=None,
            )
            if any(item.ingestion_timestamp > decision_timestamp for item in bars):
                return DataAdmission("BLOCKED", "DATA_FUTURE_DATED", latest.timestamp)
            if any(
                item.quote_timestamp is not None
                and item.quote_timestamp > decision_timestamp
                for item in bars
            ):
                return DataAdmission("BLOCKED", "DATA_QUOTE_FUTURE_DATED", latest.timestamp)
            from traderos.data.validation import validate_batch

            validate_batch(bars)
        except DataValidationError:
            return DataAdmission("BLOCKED", "DATA_INVALID", latest.timestamp)
        matching_datasets = tuple(
            item
            for item in self.dependencies.market_store.datasets()
            if item.symbol == symbol
            and item.timeframe == timeframe.value
            and item.provider == self.settings.worker_data_source
            and item.dataset_version == self.settings.worker_dataset_version
        )
        if not matching_datasets:
            return DataAdmission("BLOCKED", "DATA_NOT_QUALIFIED", latest.timestamp)
        if any(
            self._looks_like_fixture_label(value)
            for item in matching_datasets
            for value in (item.provider, item.dataset_version, item.configuration_version)
        ):
            return DataAdmission("BLOCKED", "DATA_FIXTURE_NOT_OPERATIONAL", latest.timestamp)
        if not any(
            item.quality_status.lower() in {"clean", "qualified", "verified", "accepted", "valid"}
            for item in matching_datasets
        ):
            return DataAdmission("BLOCKED", "DATA_QUALITY_NOT_ACCEPTED", latest.timestamp)
        if any(
            event.severity is QualitySeverity.ERROR
            and event.occurred_at <= decision_timestamp
            and (event.bar_timestamp is None or event.bar_timestamp <= latest.timestamp)
            and (event.source is None or event.source == self.settings.worker_data_source)
            and (event.symbol is None or event.symbol == symbol)
            and (event.timeframe is None or event.timeframe == timeframe.value)
            for event in self.dependencies.market_store.quality_events()
        ):
            return DataAdmission("BLOCKED", "DATA_QUALITY_ERROR", latest.timestamp)
        accepted_datasets = tuple(
            item
            for item in matching_datasets
            if item.quality_status.lower()
            in {"clean", "qualified", "verified", "accepted", "valid"}
        )
        if len(accepted_datasets) != 1:
            return DataAdmission("BLOCKED", "DATA_DATASET_NOT_RESOLVED", latest.timestamp)
        dataset = accepted_datasets[0]
        if latest.adjustment_policy is not dataset.adjustment_policy:
            return DataAdmission(
                "BLOCKED", "DATA_ADJUSTMENT_POLICY_MISMATCH", latest.timestamp
            )
        if dataset.start > latest.timestamp or dataset.end < latest.timestamp + timeframe.duration:
            return DataAdmission("BLOCKED", "DATA_DATASET_COVERAGE", latest.timestamp)
        if dataset.dataset_hash is None:
            return DataAdmission("BLOCKED", "DATA_IDENTITY_UNAVAILABLE", latest.timestamp)
        dataset_bars = self.dependencies.market_store.query_bars(
            symbol,
            timeframe,
            dataset.start,
            dataset.end,
            source=self.settings.worker_data_source,
            adjustment_policy=dataset.adjustment_policy,
        )
        try:
            observed_hash = market_bar_content_hash(dataset_bars)
        except ValueError:
            return DataAdmission("BLOCKED", "DATA_IDENTITY_UNAVAILABLE", latest.timestamp)
        if observed_hash != dataset.dataset_hash:
            return DataAdmission("BLOCKED", "DATA_IDENTITY_MISMATCH", latest.timestamp)
        qualification_id = self.settings.worker_dataset_qualification_id
        qualification_registry = self.dependencies.dataset_qualification_registry
        if not qualification_id or qualification_registry is None:
            return DataAdmission(
                "BLOCKED",
                "DATA_QUALIFICATION_NOT_CONFIGURED",
                latest.timestamp,
                dataset.dataset_version,
                dataset.dataset_hash,
            )
        configured_dataset_id = self.settings.worker_dataset_id
        if not configured_dataset_id:
            return DataAdmission(
                "BLOCKED",
                "DATA_QUALIFICATION_DATASET_ID_NOT_CONFIGURED",
                latest.timestamp,
                dataset.dataset_version,
                dataset.dataset_hash,
            )
        qualification = qualification_registry.read(qualification_id)
        if (
            qualification.record is None
            or qualification.status is not DatasetRecordStatus.VERIFIED
        ):
            return DataAdmission(
                "BLOCKED",
                qualification.failure_code or "DATA_QUALIFICATION_UNAVAILABLE",
                latest.timestamp,
                dataset.dataset_version,
                dataset.dataset_hash,
            )
        record = qualification.record
        if record.dataset_id != configured_dataset_id:
            return DataAdmission(
                "BLOCKED",
                "DATA_QUALIFICATION_DATASET_ID_MISMATCH",
                latest.timestamp,
                dataset.dataset_version,
                dataset.dataset_hash,
            )
        if record.checked_at > now:
            return DataAdmission(
                "BLOCKED",
                "DATA_QUALIFICATION_FUTURE_DATED",
                latest.timestamp,
                dataset.dataset_version,
                dataset.dataset_hash,
            )
        if record.dataset_content_hash != observed_hash:
            return DataAdmission(
                "BLOCKED",
                "DATA_QUALIFICATION_DATASET_MISMATCH",
                latest.timestamp,
                dataset.dataset_version,
                dataset.dataset_hash,
            )
        if (
            record.verdict is not DatasetQualificationStatus.QUALIFIED
            or record.empirical_eligibility
            is not ResearchDatasetEligibility.EMPIRICALLY_QUALIFIED_DATASET
            or record.fixture
        ):
            return DataAdmission(
                "BLOCKED",
                "DATA_QUALIFICATION_REJECTED",
                latest.timestamp,
                dataset.dataset_version,
                dataset.dataset_hash,
            )
        return DataAdmission(
            "HEALTHY",
            None,
            latest.timestamp,
            dataset.dataset_version,
            dataset.dataset_hash,
            dataset.adjustment_policy,
        )

    @staticmethod
    def _looks_like_fixture_label(value: str) -> bool:
        """Identify explicit synthetic/test labels that cannot operate PAPER."""

        normalized = value.strip().casefold()
        return "fixture" in normalized or "synthetic" in normalized

    def _resolve_strategy_eligibility(self) -> tuple[int, tuple[str, ...]]:
        if not self._strategies:
            return 0, ("STRATEGIES_NOT_CONFIGURED",)
        if self.dependencies.governance is None:
            return 0, ("STRATEGY_GOVERNANCE_UNAVAILABLE",)
        eligible = 0
        reasons: list[str] = []
        for strategy_id, version in self._strategies:
            try:
                explanation = self.dependencies.governance.explain_strategy_eligibility(
                    strategy_id, version
                )
            except LifecycleError:
                reasons.append(f"{strategy_id}@{version}:STRATEGY_NOT_ELIGIBLE")
                continue
            except Exception:
                reasons.append(f"{strategy_id}@{version}:STRATEGY_GOVERNANCE_FAILED")
                continue
            if explanation.operationally_eligible:
                eligible += 1
            else:
                reasons.extend(
                    f"{strategy_id}@{version}:{reason}" for reason in explanation.reasons
                )
        return eligible, tuple(reasons)

    def _wait_for_next_cycle(self) -> None:
        remaining = self.settings.worker_cycle_interval_seconds
        heartbeat_interval = max(0.1, min(remaining, self.settings.worker_lease_seconds / 2))
        while remaining > 0 and not self._stop_event.is_set():
            wait_for = min(remaining, heartbeat_interval)
            if self._stop_event.wait(wait_for):
                return
            now = self._now()
            renewed = self.store.heartbeat(
                self.settings.worker_workload_id,
                self.owner_id,
                now=now,
                duration=timedelta(seconds=self.settings.worker_lease_seconds),
                lease_key=self._lease_key(),
            )
            if not renewed:
                self._lease_held = False
                self._stop_event.set()
                raise WorkerError("worker lease was lost")
            self._execution_authority = renewed
            current = self.store.status(self.settings.worker_workload_id)
            if not self._save_owned_status(
                replace(current, heartbeat_at=now, worker_state=WorkerState.RUNNING),
                updated_at=now,
            ):
                raise WorkerError("worker lease was lost while updating heartbeat")
            remaining -= wait_for

    def _renew_lease_before_execution(self) -> datetime:
        """Fence risk activity behind a fresh lease owned by this worker."""

        now = self._now()
        renewed = (
            self.store.heartbeat(
                self.settings.worker_workload_id,
                self.owner_id,
                now=now,
                duration=timedelta(seconds=self.settings.worker_lease_seconds),
                lease_key=self._lease_key(),
            )
            if self._lease_held
            else None
        )
        if renewed is None:
            self._lease_held = False
            self._stop_event.set()
            raise WorkerError("worker lease was lost before paper execution")
        self._execution_authority = renewed
        current = self.store.status(self.settings.worker_workload_id)
        if not self._save_owned_status(
            replace(current, heartbeat_at=now, worker_state=WorkerState.RUNNING),
            updated_at=now,
        ):
            raise WorkerError("worker lease was lost while preparing paper execution")
        return now

    def _save_owned_status(
        self,
        status: WorkerStatus,
        *,
        updated_at: datetime,
        preserve_stop_request: bool = True,
    ) -> bool:
        """Persist status only if this worker still owns its coordination lease."""

        if not self._lease_held:
            return False
        if self.store.save_status_if_lease_owned(
            status,
            updated_at=updated_at,
            owner_id=self.owner_id,
            lease_key=self._lease_key(),
            preserve_stop_request=preserve_stop_request,
        ):
            return True
        self._lease_held = False
        self._stop_event.set()
        return False

    def _require_owned_lease_after_execution(self) -> None:
        """Fence the stale owner before it observes or records execution state."""

        if self._lease_held and self.store.lease_is_valid(
            self.settings.worker_workload_id,
            self.owner_id,
            now=self._now(),
            lease_key=self._lease_key(),
        ):
            return
        self._lease_held = False
        self._stop_event.set()
        raise WorkerError("worker lease was lost after paper execution")

    def _lease_key(self) -> str:
        """Use the paper account as the coordination scope when configured."""

        account_id = self.settings.worker_account_id
        if account_id is not None and account_id.strip():
            return f"account:{account_id.strip()}"
        return self.settings.worker_workload_id

    def _decision_key(
        self,
        *,
        data: DataAdmission,
        eligible_count: int,
        reconciliation_status: str,
        active_locks: Sequence[str],
        system_health_status: str,
        strategy_reasons: Sequence[str],
        gate_state: str,
    ) -> str:
        return hashlib.sha256(
            "|".join(
                (
                    self.settings.worker_workload_id,
                    self.settings.worker_account_id or "no-account",
                    self.settings.worker_instrument or "none",
                    self.settings.worker_timeframe or "none",
                    ",".join(f"{item[0]}@{item[1]}" for item in self._strategies),
                    data.status,
                    data.reason or "none",
                    data.latest_timestamp.isoformat()
                    if data.latest_timestamp
                    else "no-data",
                    data.dataset_version or "no-dataset",
                    data.dataset_hash or "no-dataset-hash",
                    data.adjustment_policy.value
                    if data.adjustment_policy is not None
                    else "no-adjustment-policy",
                    reconciliation_status,
                    ",".join(sorted(active_locks)) or "no-risk-lock",
                    system_health_status,
                    str(eligible_count),
                    ",".join(strategy_reasons) or "no-strategy-reason",
                    gate_state,
                    *self._decision_policy_identity(),
                )
            ).encode()
        ).hexdigest()

    def _decision_policy_identity(self) -> tuple[str, ...]:
        """Bind replay identity to effective policy and runtime configurations."""

        pipeline = self.dependencies.pipeline
        if pipeline is None:
            return (
                "no-pipeline",
                "no-risk-policy",
                "no-fusion-policy",
                "no-regime-detector",
                "no-runtime-bindings",
            )
        risk_firewall = getattr(pipeline, "risk_firewall", None)
        fusion_engine = getattr(pipeline, "fusion", None)
        fusion_policy = getattr(fusion_engine, "policy", None)
        regime_detector = getattr(pipeline, "regime_detector", None)
        bindings = getattr(pipeline, "_bindings", {})
        runtime_bindings = tuple(
            sorted(
                (
                    str(key[0]),
                    str(key[1]),
                    str(getattr(binding, "code_identity", "unknown-code-identity")),
                    str(getattr(binding.strategy, "strategy_id", "unknown-strategy")),
                    str(getattr(binding.strategy, "strategy_version", "unknown-version")),
                )
                for key, binding in bindings.items()
            )
        )
        return (
            str(getattr(risk_firewall, "policy_id", "unknown-risk-policy")),
            str(getattr(risk_firewall, "configuration_id", "unknown-risk-configuration")),
            str(getattr(fusion_policy, "configuration_id", "unknown-fusion-configuration")),
            str(getattr(regime_detector, "configuration_id", "unknown-regime-configuration")),
            json.dumps(runtime_bindings, separators=(",", ":"), sort_keys=True),
        )

    def _now(self) -> datetime:
        now = self._clock()
        require_utc(now)
        return now


def _print_status(status: WorkerStatus, *, json_output: bool = False) -> None:
    if json_output:
        print(json.dumps(status.as_dict(), sort_keys=True))
        return
    print(status.worker_state.value)
    print(f"HEALTH={status.health.value}")
    print(f"MODE={status.mode}")
    execution_path = (
        "UNKNOWN"
        if status.worker_state is WorkerState.UNKNOWN
        else "UNSUPPORTED"
        if "EXECUTION_PATH_UNSUPPORTED" in status.last_reason_codes
        else "PAPER_PIPELINE"
    )
    print(f"EXECUTION={execution_path}")
    print(f"HEARTBEAT={status.heartbeat_at.isoformat() if status.heartbeat_at else 'UNKNOWN'}")
    last_cycle = status.last_cycle_at.isoformat() if status.last_cycle_at else "UNKNOWN"
    print(f"LAST_COMPLETED_CYCLE={last_cycle}")
    print(f"DATA={status.data_status}")
    print(f"DATA_REASON={status.data_reason or 'NONE'}")
    print(
        f"DATA_LATEST={status.data_latest_at.isoformat() if status.data_latest_at else 'UNKNOWN'}"
    )
    if status.data_latest_at is None:
        print("DATA_FRESHNESS=UNKNOWN")
    else:
        age_seconds = (datetime.now(UTC) - status.data_latest_at).total_seconds()
        if age_seconds >= 0:
            print(f"DATA_FRESHNESS=AGE_SECONDS:{age_seconds:.3f}")
        else:
            print(f"DATA_FRESHNESS=FUTURE_SECONDS:{-age_seconds:.3f}")
    print(f"ELIGIBLE_STRATEGIES={status.eligible_strategy_count}")
    print(f"ACTIVE_RISK_LOCKS={','.join(status.active_risk_locks) or 'NONE'}")
    print(f"RECONCILIATION={status.reconciliation_status}")
    print(f"ACTION={status.last_decision}")
    print(f"LAST_DECISION={status.last_decision}")
    print(f"REASON_CODES={','.join(status.last_reason_codes) or 'NONE'}")
    trace = ";".join(
        f"{event.stage}:{event.status}:{event.reason or 'NONE'}" for event in status.last_trace
    )
    print(f"DECISION_TRACE={trace or 'UNKNOWN'}")
    print(f"PAPER_ORDERS={status.paper_order_count}")
    print(f"PAPER_FILLS={status.paper_fill_count}")
    print(f"STOP_REQUESTED={str(status.stop_requested).lower()}")
    if status.last_error:
        print(f"LAST_ERROR={status.last_error}")


def _print_strategy_health(
    report: StrategyHealthReport,
    *,
    json_output: bool = False,
) -> None:
    """Render read-only signal counters without implying a policy decision."""

    if json_output:
        print(json.dumps(report.as_dict(), sort_keys=True))
        return
    print(f"STRATEGY_HEALTH workload={report.workload_id}")
    print(f"CYCLE_LIMIT={report.cycle_limit}")
    print(f"CYCLES_CONSIDERED={report.cycles_considered}")
    print(f"TRUNCATED={str(report.truncated).lower()}")
    print(f"REPLAYED_CYCLES={report.replayed_cycle_count}")
    print(f"STRUCTURED_DECISIONS={report.structured_decision_count}")
    print(f"UNATTRIBUTED_DECISIONS={report.unattributed_decision_count}")
    if not report.metrics:
        print("NONE")
        return
    for metric in report.metrics:
        print(
            f"STRATEGY={metric.strategy_id}@{metric.strategy_version} "
            f"DECISIONS={metric.decision_count} "
            f"LONG={metric.long_signal_count} "
            f"SHORT={metric.short_signal_count} "
            f"FLAT={metric.flat_signal_count} "
            f"HOLD={metric.hold_signal_count} "
            f"INTENTS={metric.intent_count} "
            f"INTENT_DECISIONS={metric.intent_decision_count}"
        )


def _is_ready(status: WorkerStatus) -> bool:
    """Return whether the observed worker is safe for continued PAPER operation."""

    return (
        status.worker_state is WorkerState.RUNNING
        and status.health is WorkerHealth.HEALTHY
        and status.mode == "PAPER"
        and status.data_status == "HEALTHY"
        and status.reconciliation_status == "HEALTHY"
        and not status.stop_requested
        and status.last_error is None
    )


def _worker_run_exit_code(status: WorkerStatus) -> int:
    """Return success only for healthy or safely blocked worker outcomes."""

    if status.mode != "PAPER" or status.last_error is not None:
        return 1
    if status.health is WorkerHealth.HEALTHY:
        return 0
    if status.health is WorkerHealth.BLOCKED and status.last_decision == "NO_TRADE":
        return 0
    return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TRADEROS unattended PAPER worker")
    parser.add_argument(
        "command",
        choices=("start", "status", "ready", "stop", "run-once", "strategy-health"),
    )
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--workload", default=None)
    parser.add_argument("--max-cycles", type=int, default=None)
    parser.add_argument("--interval", type=float, default=None)
    parser.add_argument(
        "--cycle-limit",
        type=int,
        default=DEFAULT_STRATEGY_HEALTH_CYCLE_LIMIT,
        help="maximum recent cycles to inspect for strategy-health",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="render status output as one machine-readable JSON object",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    overrides: dict[str, Any] = {}
    if args.database_url is not None:
        overrides["database_url"] = args.database_url
    if args.workload is not None:
        overrides["worker_workload_id"] = args.workload
    if args.interval is not None:
        overrides["worker_cycle_interval_seconds"] = args.interval
    try:
        settings = Settings(**overrides)
        if args.command == "strategy-health":
            worker = PaperWorker.from_settings(settings)
            try:
                report = worker.store.strategy_health_report(
                    settings.worker_workload_id,
                    cycle_limit=args.cycle_limit,
                )
                _print_strategy_health(
                    report,
                    json_output=args.json,
                )
                return 0
            finally:
                worker.close()
        if args.command in {"status", "ready"}:
            worker = PaperWorker.from_settings(settings)
            try:
                status = worker.store.observed_status(
                    settings.worker_workload_id,
                    now=datetime.now(UTC),
                    lease_key=worker._lease_key(),
                )
                _print_status(status, json_output=args.json)
                return 0 if args.command == "status" or _is_ready(status) else 2
            finally:
                worker.close()
        if args.command == "stop":
            worker = PaperWorker.from_settings(settings)
            try:
                worker.store.set_stop_requested(
                    settings.worker_workload_id, True, now=datetime.now(UTC)
                )
                print(f"STOP_REQUESTED workload={settings.worker_workload_id}")
                return 0
            finally:
                worker.close()
        worker = PaperWorker.from_settings(settings)
        try:
            if args.command == "run-once":
                status = worker.run_once()
            else:
                previous_sigterm = signal.getsignal(signal.SIGTERM)

                def request_shutdown(_signum: int, _frame: object) -> None:
                    worker.request_shutdown()

                signal.signal(signal.SIGTERM, request_shutdown)
                try:
                    status = worker.run_forever(max_cycles=args.max_cycles)
                finally:
                    signal.signal(signal.SIGTERM, previous_sigterm)
            _print_status(status, json_output=args.json)
            return _worker_run_exit_code(status)
        finally:
            worker.close()
    except WorkerBusy as exc:
        print(f"BLOCKED: {exc}")
        return 2
    except (
        WorkerConfigurationError,
        WorkerError,
        PaperTradingError,
        SQLAlchemyError,
        OSError,
        ValueError,
    ) as exc:
        print(f"FAILED: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DataAdmission",
    "PaperWorker",
    "WorkerConfigurationError",
    "WorkerDependencies",
    "WorkerError",
    "WorkerHealth",
    "WorkerBusy",
    "WorkerState",
    "WorkerStatus",
    "_worker_run_exit_code",
    "main",
]
