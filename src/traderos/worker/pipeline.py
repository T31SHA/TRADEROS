"""One-cycle composition of the existing deterministic PAPER boundaries.

This module is deliberately an application adapter, not a second risk,
sizing, execution, or accounting engine.  It resolves an already governed
strategy artifact to an explicitly registered runtime implementation, then
delegates features, regime, fusion, risk, sizing, order submission, and fills
to their existing authorities.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from traderos.allocation import (
    AllocationPolicy,
    AllocationProposal,
    DeterministicPortfolioAllocator,
    ExposureSnapshot,
    PortfolioAllocationSnapshot,
    SignalSemantics,
    StrategyAllocationSignal,
)
from traderos.backtesting.models import OrderType, TimeInForce
from traderos.data.bars import MarketBar
from traderos.data.calendars import MarketCalendar, calendar_for
from traderos.data.instruments import AssetClass
from traderos.data.quality import DataQualityEvent
from traderos.data.time import require_utc
from traderos.execution.authority import ExecutionAuthority
from traderos.features.engine import FeatureEngine
from traderos.features.errors import (
    FeatureComputationError,
    FeatureConfigurationError,
    FeatureValidationError,
)
from traderos.features.models import FeatureContext, FeatureRequest
from traderos.paper.engine import PaperTradingEngine
from traderos.paper.models import PaperFill, PaperOrder, PaperQuote, PaperTradingError
from traderos.regimes import RegimeContext, RegimeDetector, RegimeEngine
from traderos.regimes.errors import RegimeError
from traderos.research.strategy_lifecycle import LifecycleError, StrategyArtifact
from traderos.research.strategy_service import EligibilityExplanation, StrategyGovernanceService
from traderos.risk import (
    MarketDataStatus,
    MarketRiskSnapshot,
    PortfolioRiskSnapshot,
    RiskAction,
    RiskAuthorization,
    RiskDecision,
    RiskDecisionStatus,
    RiskEvaluationContext,
    RiskFirewall,
    RiskReasonCode,
    SystemHealthSnapshot,
    SystemHealthStatus,
    risk_decision_integrity_id,
)
from traderos.signals import FusionContext, FusionDirection, FusionPolicy, SignalFusionEngine
from traderos.signals.errors import SignalFusionError
from traderos.strategies.base import Strategy
from traderos.strategies.errors import StrategyError
from traderos.strategies.models import StrategyContext, StrategyResult
from traderos.strategies.runtime import (
    RuntimeIdentityError,
    implementation_locator,
    runtime_content_identity,
)


@dataclass(frozen=True)
class StrategyRuntimeBinding:
    """Explicit mapping from a governed implementation to verified code.

    ``implementation_locator`` identifies where trusted application code is
    loaded. ``code_identity`` is the immutable source-manifest identity of
    that loaded code; the two values are never substituted for one another.
    """

    implementation_id: str
    implementation_version: str
    code_identity: str
    strategy: Strategy
    implementation_locator: str | None = None

    def __post_init__(self) -> None:
        if not self.implementation_id.strip() or not self.implementation_version.strip():
            raise ValueError("runtime implementation identity must not be blank")
        if not self.code_identity.strip():
            raise ValueError("runtime code identity must not be blank")
        if self.implementation_locator is None:
            object.__setattr__(
                self, "implementation_locator", implementation_locator(self.strategy)
            )
        elif not self.implementation_locator.strip():
            raise ValueError("runtime implementation locator must not be blank")


@dataclass(frozen=True)
class PipelineTraceEvent:
    """One stage result returned to the worker's durable cycle trace."""

    stage: str
    status: str
    reason: str | None = None
    metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class PaperPipelineOutcome:
    """Bounded result of one deterministic paper decision cycle."""

    action: str
    health: str
    reason_codes: tuple[str, ...]
    trace: tuple[PipelineTraceEvent, ...]
    order: PaperOrder | None = None
    fills: tuple[PaperFill, ...] = ()
    strategy_results: tuple[StrategyResult, ...] = ()
    allocation: AllocationProposal | None = None


class _FeatureRequirement(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> int: ...

    @property
    def parameters(self) -> tuple[tuple[str, int | float | str], ...]: ...


def _typed_mapping_equal(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    if set(left) != set(right):
        return False
    return all(type(left[name]) is type(right[name]) and left[name] == right[name] for name in left)


def _requirement_key(
    requirement: _FeatureRequirement,
) -> tuple[str, int, tuple[tuple[str, int | float | str], ...]]:
    return requirement.name, requirement.version, tuple(requirement.parameters)


def _feature_requests(requirements: Sequence[_FeatureRequirement]) -> tuple[FeatureRequest, ...]:
    requests: list[FeatureRequest] = []
    seen: set[tuple[str, int, tuple[tuple[str, int | float | str], ...]]] = set()
    for requirement in requirements:
        key = _requirement_key(requirement)
        if key in seen:
            continue
        seen.add(key)
        requests.append(
            FeatureRequest(
                name=key[0],
                version=key[1],
                parameters=dict(key[2]),
            )
        )
    return tuple(requests)


class PaperDecisionPipeline:
    """Compose existing research, risk, and paper interfaces for one cycle."""

    def __init__(
        self,
        *,
        paper_engine: PaperTradingEngine,
        governance: StrategyGovernanceService,
        runtime_bindings: Sequence[StrategyRuntimeBinding],
        regime_detector: RegimeDetector,
        fusion_policy: FusionPolicy,
        risk_firewall: RiskFirewall,
        allocation_policy: AllocationPolicy | None = None,
        allocator: DeterministicPortfolioAllocator | None = None,
        feature_engine: FeatureEngine | None = None,
        calendar_factory: Callable[[AssetClass], MarketCalendar] = calendar_for,
    ) -> None:
        self.paper_engine = paper_engine
        self.governance = governance
        self._bindings = {
            (item.implementation_id, item.implementation_version): item for item in runtime_bindings
        }
        if len(self._bindings) != len(tuple(runtime_bindings)):
            raise ValueError("runtime implementation identities must be unique")
        self.regime_detector = regime_detector
        self.fusion = SignalFusionEngine(fusion_policy)
        self.risk_firewall = risk_firewall
        self.allocation_policy = allocation_policy
        self.allocator = allocator or DeterministicPortfolioAllocator()
        self.features = feature_engine or FeatureEngine()
        self.calendar_factory = calendar_factory

    def run(
        self,
        *,
        account_id: str,
        strategy_keys: Sequence[tuple[str, str]],
        bars: Sequence[MarketBar],
        dataset_version: str,
        dataset_hash: str | None = None,
        now: datetime,
        execution_authority: ExecutionAuthority | None = None,
        processing_clock: Callable[[], datetime] | None = None,
        system_health: SystemHealthSnapshot | None = None,
        quality_events: Sequence[DataQualityEvent] = (),
    ) -> PaperPipelineOutcome:
        """Evaluate one completed bar and route any approval to PaperTradingEngine."""

        require_utc(now)
        processing_now = processing_clock() if processing_clock is not None else now
        require_utc(processing_now)
        trace: list[PipelineTraceEvent] = []
        if not bars:
            return self._blocked(
                ("DATA_UNAVAILABLE",),
                (PipelineTraceEvent("data", "BLOCKED", "DATA_UNAVAILABLE"),),
            )
        ordered = tuple(sorted(bars, key=lambda item: item.timestamp))
        first_scope = ordered[0]
        if any(
            item.instrument != first_scope.instrument
            or item.timeframe is not first_scope.timeframe
            or item.source != first_scope.source
            or item.adjustment_policy is not first_scope.adjustment_policy
            for item in ordered[1:]
        ):
            return self._blocked(
                ("DATA_SCOPE_MISMATCH",),
                (PipelineTraceEvent("data", "BLOCKED", "DATA_SCOPE_MISMATCH"),),
            )
        bar_keys = tuple((item.symbol, item.timeframe, item.timestamp) for item in ordered)
        if len(set(bar_keys)) != len(bar_keys):
            return self._blocked(
                ("DATA_DUPLICATE_TIMESTAMP",),
                (PipelineTraceEvent("data", "BLOCKED", "DATA_DUPLICATE_TIMESTAMP"),),
            )
        latest = ordered[-1]
        decision_timestamp = latest.timestamp + latest.timeframe.duration
        if decision_timestamp > now:
            return self._blocked(
                ("DATA_INCOMPLETE",),
                (PipelineTraceEvent("data", "BLOCKED", "DATA_INCOMPLETE"),),
            )
        if any(item.ingestion_timestamp > decision_timestamp for item in ordered):
            return self._blocked(
                ("DATA_INGESTION_FUTURE_DATED",),
                (PipelineTraceEvent("data", "BLOCKED", "DATA_INGESTION_FUTURE_DATED"),),
            )
        if processing_now - decision_timestamp > self.paper_engine.config.decision_max_age:
            return self._blocked(
                ("DATA_DECISION_STALE",),
                (PipelineTraceEvent("data", "BLOCKED", "DATA_DECISION_STALE"),),
            )
        if latest.bid is None or latest.ask is None:
            return self._blocked(
                ("DATA_QUOTE_UNAVAILABLE",),
                (PipelineTraceEvent("data", "BLOCKED", "DATA_QUOTE_UNAVAILABLE"),),
            )
        if latest.quote_timestamp is None:
            return self._blocked(
                ("DATA_QUOTE_TIMESTAMP_UNAVAILABLE",),
                (PipelineTraceEvent("data", "BLOCKED", "DATA_QUOTE_TIMESTAMP_UNAVAILABLE"),),
            )
        if any(
            item.quote_timestamp is not None and item.quote_timestamp > decision_timestamp
            for item in ordered
        ):
            return self._blocked(
                ("DATA_QUOTE_FUTURE_DATED",),
                (PipelineTraceEvent("data", "BLOCKED", "DATA_QUOTE_FUTURE_DATED"),),
            )
        quote_timestamp = latest.quote_timestamp
        if quote_timestamp > decision_timestamp:
            return self._blocked(
                ("DATA_QUOTE_FUTURE_DATED",),
                (PipelineTraceEvent("data", "BLOCKED", "DATA_QUOTE_FUTURE_DATED"),),
            )
        if processing_now - quote_timestamp > self.paper_engine.config.quote_max_age:
            return self._blocked(
                ("DATA_QUOTE_STALE",),
                (PipelineTraceEvent("data", "BLOCKED", "DATA_QUOTE_STALE"),),
            )
        trace.append(PipelineTraceEvent("data", "HEALTHY", "DATA_ADMITTED"))
        trace.append(
            PipelineTraceEvent(
                "system_health",
                system_health.status.value.upper() if system_health is not None else "UNKNOWN",
                "SYSTEM_HEALTH_ADMITTED"
                if system_health is not None
                else "SYSTEM_HEALTH_UNAVAILABLE",
            )
        )
        fills: tuple[PaperFill, ...] = ()
        pending_health_reason: str | None = None
        if system_health is None:
            pending_health_reason = "SYSTEM_HEALTH_UNAVAILABLE"
        elif system_health.status is not SystemHealthStatus.HEALTHY:
            pending_health_reason = "SYSTEM_HEALTH_NOT_HEALTHY"
        elif system_health.timestamp > processing_now:
            pending_health_reason = "SYSTEM_HEALTH_FUTURE_DATED"
        elif (
            processing_now - system_health.timestamp
            > self.risk_firewall.parameters.max_system_health_age
        ):
            pending_health_reason = "SYSTEM_HEALTH_STALE"

        if pending_health_reason is None:
            pending_processing_now = (
                processing_clock() if processing_clock is not None else processing_now
            )
            require_utc(pending_processing_now)
            if pending_processing_now - quote_timestamp > self.paper_engine.config.quote_max_age:
                pending_health_reason = "DATA_QUOTE_STALE_AT_EXECUTION"
            elif system_health is None or system_health.timestamp > pending_processing_now:
                pending_health_reason = "SYSTEM_HEALTH_UNAVAILABLE_AT_EXECUTION"
            elif (
                pending_processing_now - system_health.timestamp
                > self.risk_firewall.parameters.max_system_health_age
            ):
                pending_health_reason = "SYSTEM_HEALTH_STALE_AT_EXECUTION"
        if pending_health_reason is None:
            try:
                fills = self.paper_engine.process_quote(
                    account_id=account_id,
                    quote=PaperQuote(latest.instrument, quote_timestamp, latest.bid, latest.ask),
                    timestamp=pending_processing_now,
                    execution_authority=execution_authority,
                )
            except PaperTradingError:
                return self._blocked(
                    ("PAPER_PENDING_ORDER_PROCESSING_FAILED",),
                    (
                        *trace,
                        PipelineTraceEvent(
                            "pending_orders", "FAILED", "PAPER_PENDING_ORDER_PROCESSING_FAILED"
                        ),
                    ),
                    fills=fills,
                )
            trace.append(PipelineTraceEvent("pending_orders", "HEALTHY", f"FILLS={len(fills)}"))
        else:
            trace.append(
                PipelineTraceEvent(
                    "pending_orders",
                    "BLOCKED",
                    pending_health_reason,
                )
            )

        configured_keys = tuple(strategy_keys)
        if len(set(configured_keys)) != len(configured_keys):
            return self._blocked(
                ("STRATEGY_CONFIGURATION_DUPLICATE",),
                (
                    *trace,
                    PipelineTraceEvent(
                        "strategy_resolution",
                        "BLOCKED",
                        "STRATEGY_CONFIGURATION_DUPLICATE",
                    ),
                ),
                fills=fills,
            )

        strategies: list[Strategy] = []
        artifacts: list[StrategyArtifact] = []
        resolution_reasons: list[str] = []
        for strategy_id, strategy_version in configured_keys:
            try:
                explanation = self.governance.explain_strategy_eligibility(
                    strategy_id, strategy_version
                )
            except LifecycleError:
                resolution_reasons.append(f"{strategy_id}@{strategy_version}:STRATEGY_NOT_ELIGIBLE")
                continue
            if not explanation.operationally_eligible:
                resolution_reasons.extend(
                    f"{strategy_id}@{strategy_version}:{reason}" for reason in explanation.reasons
                )
                continue
            try:
                artifact = self.governance.get_strategy_artifact(strategy_id, strategy_version)
            except LifecycleError:
                resolution_reasons.append(
                    f"{strategy_id}@{strategy_version}:STRATEGY_GOVERNANCE_FAILED"
                )
                continue
            binding = self._bindings.get(
                (
                    artifact.implementation.implementation_id,
                    artifact.implementation.implementation_version,
                )
            )
            if binding is None:
                resolution_reasons.append(
                    f"{strategy_id}@{strategy_version}:STRATEGY_RUNTIME_UNAVAILABLE"
                )
                continue
            if artifact.dataset_identity is not None and dataset_hash is None:
                resolution_reasons.append(
                    f"{strategy_id}@{strategy_version}:STRATEGY_DATASET_IDENTITY_UNAVAILABLE"
                )
                continue
            if artifact.dataset_identity is not None and artifact.dataset_identity != dataset_hash:
                resolution_reasons.append(
                    f"{strategy_id}@{strategy_version}:STRATEGY_DATASET_IDENTITY_MISMATCH"
                )
                continue
            runtime_reason = self._validate_binding(
                artifact, binding, latest.symbol, latest.timeframe.value
            )
            if runtime_reason is not None:
                resolution_reasons.append(f"{strategy_id}@{strategy_version}:{runtime_reason}")
                continue
            strategies.append(binding.strategy)
            artifacts.append(artifact)
        if not strategies:
            return self._blocked(
                tuple(dict.fromkeys(resolution_reasons)) or ("NO_ELIGIBLE_STRATEGY",),
                (
                    *trace,
                    PipelineTraceEvent(
                        "strategy_resolution",
                        "BLOCKED",
                        resolution_reasons[0] if resolution_reasons else "NO_ELIGIBLE_STRATEGY",
                    ),
                ),
                fills=fills,
            )
        trace.append(
            PipelineTraceEvent(
                "strategy_resolution", "HEALTHY", f"ELIGIBLE_COUNT={len(strategies)}"
            )
        )

        requirements: list[_FeatureRequirement] = list(
            self.regime_detector.metadata.required_features
        )
        for strategy in strategies:
            requirements.extend(strategy.metadata.required_features)
        try:
            feature_set = self.features.compute_feature_set(
                ordered,
                _feature_requests(requirements),
                FeatureContext(
                    instrument=latest.instrument,
                    timeframe=latest.timeframe,
                    source_dataset_version=dataset_version,
                    start=ordered[0].timestamp,
                    end=latest.timestamp,
                    computation_timestamp=decision_timestamp,
                ),
            )
        except (
            FeatureComputationError,
            FeatureConfigurationError,
            FeatureValidationError,
        ) as exc:
            return self._blocked(
                ("FEATURE_COMPUTATION_FAILED",),
                (*trace, PipelineTraceEvent("features", "BLOCKED", str(exc))),
                fills=fills,
            )
        trace.append(PipelineTraceEvent("features", "HEALTHY"))
        available_events = tuple(
            event
            for event in quality_events
            if event.occurred_at <= decision_timestamp
            and (event.bar_timestamp is None or event.bar_timestamp <= latest.timestamp)
            and (event.source is None or event.source == latest.source)
            and (event.symbol is None or event.symbol == latest.symbol)
            and (event.timeframe is None or event.timeframe == latest.timeframe.value)
        )
        try:
            regime = RegimeEngine(self.regime_detector).evaluate(
                RegimeContext(
                    bar=latest,
                    decision_timestamp=decision_timestamp,
                    feature_observations=feature_set.observations,
                    calendar=self.calendar_factory(latest.asset_class),
                    detector_parameters=self.regime_detector.parameters,
                    quality_events=available_events,
                )
            )
        except RegimeError as exc:
            return self._failed(
                ("REGIME_EVALUATION_FAILED",),
                (*trace, PipelineTraceEvent("regime", "FAILED", str(exc))),
                fills=fills,
            )
        trace.append(PipelineTraceEvent("regime", "HEALTHY", regime.state_id))

        portfolio = self.paper_engine.portfolio_risk_snapshot(account_id)
        account = self.paper_engine.store.account(account_id)
        position = self.paper_engine.store.position(account_id, latest.symbol)
        try:
            results = tuple(
                strategy.on_bar(
                    StrategyContext(
                        event_timestamp=decision_timestamp,
                        bar=latest,
                        feature_observations=feature_set.observations,
                        position_quantity=(
                            position.quantity if position is not None else Decimal("0")
                        ),
                        cash=account.cash,
                        equity=portfolio.equity or Decimal("0"),
                        parameters=strategy.parameters,
                        strategy_id=strategy.strategy_id,
                        strategy_version=strategy.strategy_version,
                    )
                )
                for strategy in strategies
            )
        except StrategyError as exc:
            return self._failed(
                ("STRATEGY_EVALUATION_FAILED",),
                (*trace, PipelineTraceEvent("strategy_evaluation", "FAILED", str(exc))),
                fills=fills,
            )
        for result in results:
            trace.append(
                PipelineTraceEvent(
                    "strategy_decision",
                    "HEALTHY",
                    (
                        f"{result.signal.strategy_id}@{result.signal.strategy_version}"
                        f":direction={result.signal.direction.value}"
                        f":intents={len(result.order_intents)}"
                        f":reason={result.signal.reason}"
                    ),
                    (
                        ("strategy_id", result.signal.strategy_id),
                        ("strategy_version", result.signal.strategy_version),
                        ("direction", result.signal.direction.value),
                        ("intent_count", str(len(result.order_intents))),
                    ),
                )
            )
        trace.append(PipelineTraceEvent("strategy_evaluation", "HEALTHY"))
        try:
            intent = self.fusion.fuse(
                FusionContext(
                    latest.instrument,
                    latest.timeframe,
                    decision_timestamp,
                    tuple(result.signal for result in results),
                    regime,
                )
            )
        except SignalFusionError as exc:
            return self._failed(
                ("FUSION_FAILED",),
                (*trace, PipelineTraceEvent("fusion", "FAILED", str(exc))),
                fills=fills,
            )
        if intent.direction not in {FusionDirection.LONG, FusionDirection.SHORT}:
            reason = f"FUSION_{intent.status.value.upper()}"
            return PaperPipelineOutcome(
                action="NO_TRADE",
                health="HEALTHY",
                reason_codes=(reason,),
                trace=(*trace, PipelineTraceEvent("fusion", "BLOCKED", reason)),
                fills=fills,
                strategy_results=results,
            )
        matching_intent = any(
            order_intent.side.value
            == ("buy" if intent.direction is FusionDirection.LONG else "sell")
            for result in results
            for order_intent in result.order_intents
        )
        if not matching_intent:
            reason = "STRATEGY_NO_ACTIONABLE_INTENT"
            return PaperPipelineOutcome(
                action="NO_TRADE",
                health="HEALTHY",
                reason_codes=(reason,),
                trace=(*trace, PipelineTraceEvent("strategy_intent", "BLOCKED", reason)),
                fills=fills,
                strategy_results=results,
            )
        trace.append(PipelineTraceEvent("fusion", "HEALTHY", intent.intent_id))

        allocation_signals: list[StrategyAllocationSignal] = []
        for result, artifact in zip(results, artifacts, strict=True):
            expected_side = "buy" if result.signal.direction.value == "long" else "sell"
            for order_intent in result.order_intents:
                if order_intent.side.value != expected_side:
                    continue
                price = latest.ask if expected_side == "buy" else latest.bid
                assert price is not None
                allocation_signals.append(
                    StrategyAllocationSignal(
                        source_signal_id=result.signal.signal_id,
                        strategy_id=result.signal.strategy_id,
                        strategy_version=result.signal.strategy_version,
                        artifact_hash=artifact.artifact_hash,
                        strategy_family_id=artifact.family_id,
                        instrument=latest.instrument,
                        timeframe=latest.timeframe,
                        event_timestamp=latest.timestamp,
                        availability_timestamp=latest.ingestion_timestamp,
                        decision_timestamp=decision_timestamp,
                        direction=result.signal.direction,
                        semantics=SignalSemantics.DIRECTIONAL_NOTIONAL_REQUEST,
                        reason=result.signal.reason,
                        configuration_identity=(
                            artifact.configuration_identity or artifact.artifact_hash
                        ),
                        requested_notional=order_intent.quantity * price,
                        target_preference=None,
                    )
                )
        try:
            allocation_snapshot = self._allocation_snapshot_with_reservations(
                account_id, portfolio, latest
            )
        except ValueError as exc:
            return self._blocked(
                ("ALLOCATION_PORTFOLIO_INPUT_UNAVAILABLE",),
                (*trace, PipelineTraceEvent("allocation", "BLOCKED", str(exc))),
                fills=fills,
            )
        allocation = self.allocator.evaluate(
            tuple(allocation_signals), allocation_snapshot, self.allocation_policy
        )
        allocation_trace = PipelineTraceEvent(
            "allocation",
            "HEALTHY" if allocation.actionable else "BLOCKED",
            (
                allocation.constraint_reasons[0]
                if allocation.constraint_reasons
                else allocation.decision_id
            ),
            self._allocation_trace_metadata(allocation),
        )
        if not allocation.actionable or (
            allocation.max_new_notional <= 0 and allocation.max_reduction_notional <= 0
        ):
            return PaperPipelineOutcome(
                action="NO_TRADE",
                health="BLOCKED",
                reason_codes=tuple(allocation.constraint_reasons) or ("ALLOCATION_REJECTED",),
                trace=(*trace, allocation_trace),
                fills=fills,
                strategy_results=results,
                allocation=allocation,
            )
        trace.append(allocation_trace)

        # Allocation is a proposal against a coherent revision.  Re-read both
        # the revision and governance state before the existing firewall and
        # paper transaction consume it.
        current_portfolio = self.paper_engine.portfolio_risk_snapshot(account_id)
        if not allocation.validate_snapshot(
            account_id=account_id, revision=current_portfolio.revision
        ):
            return self._blocked(
                ("ALLOCATION_PORTFOLIO_REVISION_STALE",),
                (
                    *trace,
                    PipelineTraceEvent(
                        "allocation_revalidation",
                        "BLOCKED",
                        "ALLOCATION_PORTFOLIO_REVISION_STALE",
                    ),
                ),
                fills=fills,
            )
        for strategy, artifact in zip(strategies, artifacts, strict=True):
            current_explanation: EligibilityExplanation | None = None
            current_artifact: StrategyArtifact | None = None
            try:
                current_explanation = self.governance.explain_strategy_eligibility(
                    strategy.strategy_id, strategy.strategy_version
                )
                current_artifact = self.governance.get_strategy_artifact(
                    strategy.strategy_id, strategy.strategy_version
                )
            except LifecycleError:
                current_explanation = None
                current_artifact = None
            if (
                current_explanation is None
                or not current_explanation.operationally_eligible
                or current_artifact is None
                or current_artifact.artifact_hash != artifact.artifact_hash
            ):
                return self._blocked(
                    ("ALLOCATION_STRATEGY_GOVERNANCE_CHANGED",),
                    (
                        *trace,
                        PipelineTraceEvent(
                            "allocation_revalidation",
                            "BLOCKED",
                            "ALLOCATION_STRATEGY_GOVERNANCE_CHANGED",
                        ),
                    ),
                    fills=fills,
                )
        portfolio = current_portfolio
        processing_now = processing_clock() if processing_clock is not None else processing_now
        require_utc(processing_now)
        if processing_now - quote_timestamp > self.paper_engine.config.quote_max_age:
            return self._blocked(
                ("DATA_QUOTE_STALE_AT_EXECUTION",),
                (
                    *trace,
                    PipelineTraceEvent(
                        "execution_freshness", "BLOCKED", "DATA_QUOTE_STALE_AT_EXECUTION"
                    ),
                ),
                fills=fills,
            )
        if system_health is None or system_health.timestamp > processing_now:
            return self._blocked(
                ("RISK_SYSTEM_UNHEALTHY",),
                (
                    *trace,
                    PipelineTraceEvent("execution_freshness", "BLOCKED", "RISK_SYSTEM_UNHEALTHY"),
                ),
                fills=fills,
            )
        if (
            processing_now - system_health.timestamp
            > self.risk_firewall.parameters.max_system_health_age
        ):
            return self._blocked(
                ("RISK_SYSTEM_HEALTH_STALE",),
                (
                    *trace,
                    PipelineTraceEvent(
                        "execution_freshness", "BLOCKED", "RISK_SYSTEM_HEALTH_STALE"
                    ),
                ),
                fills=fills,
            )
        decision = self.risk_firewall.evaluate(
            RiskEvaluationContext(
                decision_timestamp=decision_timestamp,
                processing_timestamp=processing_now,
                intent=intent,
                regime_state=regime,
                market=MarketRiskSnapshot(
                    latest.instrument,
                    quote_timestamp,
                    MarketDataStatus.VALID,
                    latest.bid,
                    latest.ask,
                ),
                portfolio=portfolio,
                system_health=system_health,
            )
        )
        if decision.status is not RiskDecisionStatus.APPROVE:
            reasons = tuple(f"RISK_{reason.value.upper()}" for reason in decision.reason_codes)
            safety_blocked = {
                RiskReasonCode.SYSTEM_UNHEALTHY,
                RiskReasonCode.SYSTEM_HEALTH_STALE,
                RiskReasonCode.DATA_UNAVAILABLE,
                RiskReasonCode.DATA_STALE,
                RiskReasonCode.DATA_INVALID,
                RiskReasonCode.REGIME_UNAVAILABLE,
                RiskReasonCode.MARKET_INACTIVE,
                RiskReasonCode.RISK_LOCK_ACTIVE,
                RiskReasonCode.DRAWDOWN_LOCKED,
                RiskReasonCode.DAILY_LOSS_LIMIT,
                RiskReasonCode.PORTFOLIO_INVALID,
                RiskReasonCode.STALE_RISK_SNAPSHOT,
                RiskReasonCode.KILL_SWITCH_ACTIVE,
                RiskReasonCode.FUTURE_DATED_INPUT,
                RiskReasonCode.PRICE_INVALID,
                RiskReasonCode.CROSSED_MARKET,
                RiskReasonCode.SPREAD_TOO_WIDE,
                RiskReasonCode.LIQUIDITY_STRESSED,
                RiskReasonCode.DRAWDOWN_LIMIT,
                RiskReasonCode.RISK_DAY_MISMATCH,
            }
            return PaperPipelineOutcome(
                action="NO_TRADE",
                health=(
                    "BLOCKED"
                    if any(reason in safety_blocked for reason in decision.reason_codes)
                    else "HEALTHY"
                ),
                reason_codes=reasons or ("RISK_REJECTED",),
                trace=(
                    *trace,
                    PipelineTraceEvent(
                        "risk_firewall",
                        "BLOCKED",
                        ",".join(reasons),
                        self._risk_trace_metadata(decision),
                    ),
                ),
                fills=fills,
                strategy_results=results,
            )
        if allocation.max_new_notional > 0 and allocation.max_reduction_notional > 0:
            return self._blocked(
                ("ALLOCATION_REVERSAL_REQUIRES_SPLIT_ORDER",),
                (
                    *trace,
                    PipelineTraceEvent(
                        "allocation_execution_bounds",
                        "BLOCKED",
                        "ALLOCATION_REVERSAL_REQUIRES_SPLIT_ORDER",
                    ),
                ),
                fills=fills,
            )
        try:
            decision = self._bound_risk_decision(decision, allocation)
        except ValueError:
            return self._blocked(
                ("ALLOCATION_NO_AUTHORIZED_DELTA",),
                (
                    *trace,
                    PipelineTraceEvent(
                        "allocation_execution_bounds", "BLOCKED", "ALLOCATION_NO_AUTHORIZED_DELTA"
                    ),
                ),
                fills=fills,
            )
        trace.append(
            PipelineTraceEvent(
                "risk_firewall",
                "HEALTHY",
                decision.decision_id,
                self._risk_trace_metadata(decision),
            )
        )
        submit_now = processing_clock() if processing_clock is not None else processing_now
        require_utc(submit_now)
        if submit_now - quote_timestamp > self.paper_engine.config.quote_max_age:
            return self._blocked(
                ("DATA_QUOTE_STALE_BEFORE_MUTATION",),
                (
                    *trace,
                    PipelineTraceEvent(
                        "execution_freshness",
                        "BLOCKED",
                        "DATA_QUOTE_STALE_BEFORE_MUTATION",
                    ),
                ),
                fills=fills,
            )
        if submit_now < system_health.timestamp or (
            submit_now - system_health.timestamp
            > self.risk_firewall.parameters.max_system_health_age
        ):
            return self._blocked(
                ("RISK_SYSTEM_HEALTH_STALE_BEFORE_MUTATION",),
                (
                    *trace,
                    PipelineTraceEvent(
                        "execution_freshness",
                        "BLOCKED",
                        "RISK_SYSTEM_HEALTH_STALE_BEFORE_MUTATION",
                    ),
                ),
                fills=fills,
            )
        try:
            order = self.paper_engine.submit(
                account_id=account_id,
                idempotency_key=f"worker:{account_id}:{intent.intent_id}",
                risk_decision=decision,
                quote=PaperQuote(latest.instrument, quote_timestamp, latest.bid, latest.ask),
                timestamp=submit_now,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.GTC,
                execution_authority=execution_authority,
            )
        except PaperTradingError as exc:
            return PaperPipelineOutcome(
                action="NO_TRADE",
                health="BLOCKED",
                reason_codes=("PAPER_SUBMISSION_REJECTED",),
                trace=(*trace, PipelineTraceEvent("paper_submission", "BLOCKED", str(exc))),
                fills=fills,
                strategy_results=results,
                allocation=allocation,
            )
        return PaperPipelineOutcome(
            action="ORDER_SUBMITTED",
            health="HEALTHY",
            reason_codes=("ORDER_SUBMITTED",),
            trace=(*trace, PipelineTraceEvent("paper_submission", "HEALTHY", order.order_id)),
            order=order,
            fills=fills,
            strategy_results=results,
            allocation=allocation,
        )

    @staticmethod
    def _allocation_snapshot(
        account_id: str,
        portfolio: PortfolioRiskSnapshot,
        latest: MarketBar,
    ) -> PortfolioAllocationSnapshot:
        if portfolio.account_currency not in {
            latest.instrument.trading_currency,
            latest.instrument.quote_currency,
        }:
            raise ValueError("ALLOCATION_CONVERSION_INPUT_UNAVAILABLE")
        positions = tuple(
            ExposureSnapshot(
                instrument=item.instrument,
                signed_notional=item.net_notional,
                currency=portfolio.account_currency,
            )
            for item in portfolio.positions
        )
        reservations: list[ExposureSnapshot] = []
        # The current worker scope has one quote.  An outstanding order on a
        # different instrument cannot be valued honestly without a conversion
        # input and therefore blocks the proposal instead of hiding capacity.
        return PortfolioAllocationSnapshot(
            account_id=account_id,
            account_currency=portfolio.account_currency,
            timestamp=portfolio.timestamp,
            revision=portfolio.revision,
            positions=positions,
            reservations=tuple(reservations),
        )

    def _allocation_snapshot_with_reservations(
        self,
        account_id: str,
        portfolio: PortfolioRiskSnapshot,
        latest: MarketBar,
    ) -> PortfolioAllocationSnapshot:
        """Build a snapshot including durable open-order reservations."""

        snapshot = self._allocation_snapshot(account_id, portfolio, latest)
        reservations: list[ExposureSnapshot] = []
        for order in self.paper_engine.store.orders(account_id):
            if not order.is_open or order.remaining_quantity <= 0:
                continue
            if order.instrument != latest.instrument:
                raise ValueError("ALLOCATION_RESERVATION_VALUATION_UNAVAILABLE")
            price = latest.ask if order.side.value == "buy" else latest.bid
            assert price is not None
            signed = order.remaining_quantity * price
            if order.side.value == "sell":
                signed = -signed
            reservations.append(
                ExposureSnapshot(
                    instrument=order.instrument,
                    signed_notional=signed,
                    currency=portfolio.account_currency,
                    reservation_id=order.order_id,
                )
            )
        return replace(snapshot, reservations=tuple(reservations))

    @staticmethod
    def _bound_risk_decision(decision: RiskDecision, proposal: AllocationProposal) -> RiskDecision:
        if decision.authorization is None:
            raise ValueError("approved risk decision has no authorization")
        authorization = decision.authorization
        if authorization.action is RiskAction.NEW_OR_INCREASE:
            if proposal.max_new_notional <= 0:
                raise ValueError("allocation does not permit new risk")
            bounded = RiskAuthorization(
                action=authorization.action,
                max_new_notional=min(authorization.max_new_notional, proposal.max_new_notional),
                max_loss_at_stop=authorization.max_loss_at_stop,
                max_reduction_notional=authorization.max_reduction_notional,
                require_pretrade_sizing=authorization.require_pretrade_sizing,
                stop_risk_supported=authorization.stop_risk_supported,
            )
        else:
            if proposal.max_reduction_notional <= 0:
                raise ValueError("allocation does not permit reduction")
            bounded = RiskAuthorization(
                action=authorization.action,
                max_new_notional=authorization.max_new_notional,
                max_loss_at_stop=authorization.max_loss_at_stop,
                max_reduction_notional=min(
                    authorization.max_reduction_notional, proposal.max_reduction_notional
                ),
                require_pretrade_sizing=authorization.require_pretrade_sizing,
                stop_risk_supported=authorization.stop_risk_supported,
            )
        return replace(
            decision,
            authorization=bounded,
            decision_id=risk_decision_integrity_id(replace(decision, authorization=bounded)),
        )

    @staticmethod
    def _allocation_trace_metadata(
        proposal: AllocationProposal,
    ) -> tuple[tuple[str, str], ...]:
        payload = {
            "decision_id": proposal.decision_id,
            "policy_id": proposal.policy_id,
            "policy_version": proposal.policy_version,
            "configuration_id": proposal.configuration_id,
            "snapshot_revision": proposal.snapshot_revision,
            "status": proposal.status.value,
            "constraints_applied": proposal.constraints_applied,
            "max_new_notional": str(proposal.max_new_notional),
            "max_reduction_notional": str(proposal.max_reduction_notional),
            "constraints": proposal.constraint_reasons,
            "targets": [
                {
                    "symbol": item.instrument.canonical_symbol,
                    "existing": str(item.existing_signed_exposure),
                    "reserved": str(item.reserved_signed_exposure),
                    "target": str(item.proposed_target_exposure),
                    "delta": str(item.required_delta),
                    "reasons": item.reason_codes,
                }
                for item in proposal.targets
            ],
            "contributions": [
                {
                    "strategy": f"{item.strategy_id}@{item.strategy_version}",
                    "family": item.strategy_family_id,
                    "symbol": item.instrument.canonical_symbol,
                    "requested": str(item.requested_delta),
                    "proposed": str(item.proposed_delta),
                    "reasons": item.reason_codes,
                }
                for item in proposal.contributions
            ],
        }
        return (("allocation", json.dumps(payload, sort_keys=True, separators=(",", ":"))),)

    @staticmethod
    def _validate_binding(
        artifact: StrategyArtifact,
        binding: StrategyRuntimeBinding,
        symbol: str,
        timeframe: str,
    ) -> str | None:
        strategy = binding.strategy
        if binding.implementation_locator != implementation_locator(strategy):
            return "STRATEGY_IMPLEMENTATION_LOCATOR_MISMATCH"
        if binding.code_identity != artifact.code_identity:
            return "STRATEGY_CODE_IDENTITY_MISMATCH"
        try:
            verified_identity = runtime_content_identity(strategy)
        except RuntimeIdentityError:
            return "STRATEGY_CODE_IDENTITY_UNAVAILABLE"
        if binding.code_identity != verified_identity:
            return "STRATEGY_CODE_IDENTITY_UNVERIFIED"
        if (strategy.strategy_id, strategy.strategy_version) != (
            artifact.strategy_id,
            artifact.strategy_version,
        ):
            return "STRATEGY_IDENTITY_MISMATCH"
        if symbol not in artifact.instruments or timeframe not in artifact.timeframes:
            return "STRATEGY_SCOPE_MISMATCH"
        if not _typed_mapping_equal(
            {item.name: item.value for item in artifact.parameters},
            dict(strategy.parameters),
        ):
            return "STRATEGY_PARAMETERS_MISMATCH"
        artifact_features: set[tuple[str, str, tuple[tuple[str, object], ...]]] = {
            (
                item.name,
                str(item.version),
                tuple((parameter.name, parameter.value) for parameter in item.parameters),
            )
            for item in artifact.features
        }
        runtime_features = {
            (name, str(version), parameters)
            for name, version, parameters in (
                _requirement_key(item) for item in strategy.metadata.required_features
            )
        }
        if artifact_features != runtime_features:
            return "STRATEGY_FEATURES_MISMATCH"
        return None

    @staticmethod
    def _blocked(
        reasons: tuple[str, ...],
        trace: tuple[PipelineTraceEvent, ...],
        *,
        fills: tuple[PaperFill, ...] = (),
    ) -> PaperPipelineOutcome:
        return PaperPipelineOutcome(
            action="NO_TRADE",
            health="BLOCKED",
            reason_codes=tuple(dict.fromkeys(reasons)),
            trace=trace,
            fills=fills,
        )

    @staticmethod
    def _failed(
        reasons: tuple[str, ...],
        trace: tuple[PipelineTraceEvent, ...],
        *,
        fills: tuple[PaperFill, ...] = (),
    ) -> PaperPipelineOutcome:
        return PaperPipelineOutcome(
            action="NO_TRADE",
            health="FAILED",
            reason_codes=tuple(dict.fromkeys(reasons)),
            trace=trace,
            fills=fills,
        )

    @staticmethod
    def _risk_trace_metadata(decision: RiskDecision) -> tuple[tuple[str, str], ...]:
        """Serialize the bounded firewall result into the existing cycle trace."""

        authorization = decision.authorization
        authorization_payload = None
        if authorization is not None:
            authorization_payload = {
                "action": authorization.action.value,
                "max_loss_at_stop": str(authorization.max_loss_at_stop),
                "max_new_notional": str(authorization.max_new_notional),
                "max_reduction_notional": str(authorization.max_reduction_notional),
                "require_pretrade_sizing": authorization.require_pretrade_sizing,
                "stop_risk_supported": authorization.stop_risk_supported,
            }
        checks = [
            {
                "check_id": check.check_id,
                "limit": check.limit,
                "observed": check.observed,
                "passed": check.passed,
                "reason_code": check.reason_code.value if check.reason_code else None,
            }
            for check in decision.checks
        ]
        return (
            ("decision_id", decision.decision_id),
            ("status", decision.status.value),
            ("policy_id", decision.policy_id),
            ("policy_version", decision.policy_version),
            ("configuration_id", decision.configuration_id),
            ("checks", json.dumps(checks, sort_keys=True, separators=(",", ":"))),
            (
                "authorization",
                json.dumps(authorization_payload, sort_keys=True, separators=(",", ":")),
            ),
        )


__all__ = [
    "PaperDecisionPipeline",
    "PaperPipelineOutcome",
    "PipelineTraceEvent",
    "StrategyRuntimeBinding",
]
