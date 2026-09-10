"""Immutable normalization, policy, context, and fused-intent contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite
from typing import Protocol

from traderos.data.instruments import AssetClass, Instrument
from traderos.data.time import require_utc
from traderos.data.timeframes import Timeframe
from traderos.regimes.models import LiquidityRegime, RegimeState, TrendRegime, VolatilityRegime
from traderos.signals.errors import (
    DuplicateStrategySignalError,
    FusionCausalityError,
    FutureRegimeError,
    FutureSignalError,
    IncompatibleSignalError,
)
from traderos.strategies.models import FeatureRequirement, StrategySignal


class FusionDirection(StrEnum):
    """Explicit fusion semantics; HOLD and FLAT intentionally differ."""

    LONG = "long"
    SHORT = "short"
    FLAT = "flat"
    HOLD = "hold"
    UNAVAILABLE = "unavailable"


class FusionStatus(StrEnum):
    """Why a fused decision has its direction or deliberately has none."""

    DIRECTIONAL = "directional"
    FLAT = "flat"
    HOLD = "hold"
    NO_CONSENSUS = "no_consensus"
    REGIME_UNAVAILABLE = "regime_unavailable"
    ALL_SIGNALS_UNAVAILABLE = "all_signals_unavailable"


class EligibilityState(StrEnum):
    """Per-signal participation state retained for an audit trail."""

    ELIGIBLE = "eligible"
    UNAVAILABLE = "unavailable"
    REGIME_INCOMPATIBLE = "regime_incompatible"
    DUPLICATE = "duplicate"
    REGIME_UNAVAILABLE = "regime_unavailable"


@dataclass(frozen=True)
class NormalizedSignal:
    """Canonical, non-probabilistic representation of a Phase 4 signal."""

    source_signal_id: str
    strategy_id: str
    strategy_version: str
    instrument: Instrument
    asset_class: AssetClass
    timeframe: Timeframe
    decision_timestamp: datetime
    direction: FusionDirection
    reason: str
    feature_provenance: tuple[FeatureRequirement, ...]
    deterministic_score: float | None = None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        require_utc(self.decision_timestamp)
        if self.asset_class is not self.instrument.asset_class:
            raise ValueError("normalized signal asset class differs from instrument")
        if not all(
            value.strip()
            for value in (
                self.source_signal_id,
                self.strategy_id,
                self.strategy_version,
                self.reason,
            )
        ):
            raise ValueError("normalized signal identity and reason must not be blank")
        if self.deterministic_score is not None and not isfinite(self.deterministic_score):
            raise ValueError("normalized signal score must be finite")
        if self.direction is FusionDirection.UNAVAILABLE:
            if self.unavailable_reason is None or not self.unavailable_reason.strip():
                raise ValueError("unavailable normalized signals require an unavailable reason")
        elif self.unavailable_reason is not None:
            raise ValueError("available normalized signals must not include an unavailable reason")

    @classmethod
    def from_strategy_signal(cls, signal: StrategySignal) -> NormalizedSignal:
        """Preserve Phase 4 semantics without manufacturing confidence."""

        direction = FusionDirection(signal.direction.value)
        return cls(
            source_signal_id=signal.signal_id,
            strategy_id=signal.strategy_id,
            strategy_version=signal.strategy_version,
            instrument=signal.instrument,
            asset_class=signal.instrument.asset_class,
            timeframe=signal.timeframe,
            decision_timestamp=signal.timestamp,
            direction=direction,
            reason=signal.reason,
            feature_provenance=signal.feature_provenance,
            deterministic_score=signal.score,
        )

    @property
    def strategy_key(self) -> tuple[str, str]:
        """Identity that gets at most one vote in a fusion decision."""

        return self.strategy_id, self.strategy_version


@dataclass(frozen=True)
class RegimeCompatibilityRule:
    """Declarative constraints for one strategy/direction and regime combination."""

    strategy_id: str
    strategy_version: str
    allowed_directions: frozenset[FusionDirection] | None = None
    allowed_trends: frozenset[TrendRegime] | None = None
    allowed_volatility: frozenset[VolatilityRegime] | None = None
    allowed_liquidity: frozenset[LiquidityRegime] | None = None

    def __post_init__(self) -> None:
        if not self.strategy_id.strip() or not self.strategy_version.strip():
            raise ValueError("compatibility rule strategy identity must not be blank")
        for values in (
            self.allowed_directions,
            self.allowed_trends,
            self.allowed_volatility,
            self.allowed_liquidity,
        ):
            if values is not None and not values:
                raise ValueError("compatibility rule constraints must not be empty")

    def applies_to(self, signal: NormalizedSignal) -> bool:
        """Return whether the rule governs this strategy and direction."""

        return signal.strategy_key == (self.strategy_id, self.strategy_version) and (
            self.allowed_directions is None or signal.direction in self.allowed_directions
        )

    def allows(self, regime: RegimeState) -> bool:
        """Return whether the supplied known regime satisfies every constraint."""

        return (
            (self.allowed_trends is None or regime.trend in self.allowed_trends)
            and (self.allowed_volatility is None or regime.volatility in self.allowed_volatility)
            and (self.allowed_liquidity is None or regime.liquidity in self.allowed_liquidity)
        )

    def canonical(self) -> dict[str, object]:
        """Return a stable serializable rule form for configuration identity."""

        def values(items: frozenset[StrEnum] | None) -> list[str] | None:
            return None if items is None else sorted(item.value for item in items)

        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "allowed_directions": values(self.allowed_directions),
            "allowed_trends": values(self.allowed_trends),
            "allowed_volatility": values(self.allowed_volatility),
            "allowed_liquidity": values(self.allowed_liquidity),
        }


@dataclass(frozen=True)
class FusionPolicyMetadata:
    """Versioned declaration for a deterministic fusion policy."""

    policy_id: str
    policy_version: str
    parameter_schema: tuple[str, ...]
    description: str

    def __post_init__(self) -> None:
        if (
            not self.policy_id.strip()
            or not self.policy_version.strip()
            or not self.description.strip()
        ):
            raise ValueError("fusion policy identity and description must not be blank")
        if len(set(self.parameter_schema)) != len(self.parameter_schema):
            raise ValueError("fusion policy parameter schema names must be unique")


@dataclass(frozen=True)
class SignalExclusion:
    """An auditable reason a normalized signal did not contribute."""

    signal: NormalizedSignal
    eligibility: EligibilityState
    reason: str

    def __post_init__(self) -> None:
        if self.eligibility is EligibilityState.ELIGIBLE or not self.reason.strip():
            raise ValueError("signal exclusions require a non-eligible state and reason")


@dataclass(frozen=True)
class FusionContext:
    """One strict, single-instrument decision-time fusion input boundary."""

    instrument: Instrument
    timeframe: Timeframe
    decision_timestamp: datetime
    signals: tuple[StrategySignal | NormalizedSignal, ...]
    regime_state: RegimeState | None

    def __post_init__(self) -> None:
        require_utc(self.decision_timestamp)
        normalized = tuple(
            signal
            if isinstance(signal, NormalizedSignal)
            else NormalizedSignal.from_strategy_signal(signal)
            for signal in self.signals
        )
        object.__setattr__(self, "signals", normalized)
        if not normalized:
            raise ValueError("fusion context requires at least one strategy signal")
        source_ids_by_strategy: dict[tuple[str, str], set[str]] = {}
        for signal in normalized:
            if (
                signal.instrument != self.instrument
                or signal.asset_class is not self.instrument.asset_class
            ):
                raise IncompatibleSignalError(
                    "signal instrument or asset class differs from fusion scope"
                )
            if signal.timeframe is not self.timeframe:
                raise IncompatibleSignalError("signal timeframe differs from fusion scope")
            if signal.decision_timestamp > self.decision_timestamp:
                raise FutureSignalError("future strategy signal entered fusion context")
            if signal.decision_timestamp < self.decision_timestamp:
                raise FusionCausalityError(
                    "strategy signals must match the fusion decision timestamp"
                )
            source_ids_by_strategy.setdefault(signal.strategy_key, set()).add(
                signal.source_signal_id
            )
        if any(len(source_ids) > 1 for source_ids in source_ids_by_strategy.values()):
            raise DuplicateStrategySignalError(
                "one strategy instance supplied conflicting signals for one fusion decision"
            )
        if self.regime_state is not None:
            regime = self.regime_state
            if (
                regime.instrument != self.instrument
                or regime.asset_class is not self.instrument.asset_class
            ):
                raise IncompatibleSignalError(
                    "regime instrument or asset class differs from fusion scope"
                )
            if regime.timeframe is not self.timeframe:
                raise IncompatibleSignalError("regime timeframe differs from fusion scope")
            if regime.decision_timestamp > self.decision_timestamp:
                raise FutureRegimeError("future regime state entered fusion context")
            if regime.decision_timestamp < self.decision_timestamp:
                raise FusionCausalityError("regime state must match the fusion decision timestamp")

    @property
    def asset_class(self) -> AssetClass:
        return self.instrument.asset_class


@dataclass(frozen=True)
class PolicyDecision:
    """Pure direction/status result returned by a fusion policy."""

    direction: FusionDirection
    status: FusionStatus


class FusionPolicy(Protocol):
    """Controlled, versioned policy contract; policies cannot execute code from config."""

    policy_id: str
    policy_version: str

    @property
    def metadata(self) -> FusionPolicyMetadata:
        """Return immutable policy identity and schema."""

    @property
    def parameters(self) -> Mapping[str, int | float | str]:
        """Return validated effective policy parameters."""

    @property
    def compatibility_rules(self) -> tuple[RegimeCompatibilityRule, ...]:
        """Return explicit, immutable strategy/regime constraints."""

    @property
    def configuration_id(self) -> str:
        """Return the stable configuration identity."""

    def decide(self, signals: tuple[NormalizedSignal, ...]) -> PolicyDecision:
        """Resolve already eligible signals deterministically."""


@dataclass(frozen=True)
class UnifiedTradeIntent:
    """Auditable fused exposure instruction with deliberately no quantity field."""

    intent_id: str
    instrument: Instrument
    asset_class: AssetClass
    timeframe: Timeframe
    decision_timestamp: datetime
    direction: FusionDirection
    status: FusionStatus
    policy_id: str
    policy_version: str
    configuration_id: str
    regime_state_id: str | None
    regime_detector_id: str | None
    regime_detector_version: str | None
    eligible_signals: tuple[NormalizedSignal, ...]
    excluded_signals: tuple[SignalExclusion, ...]
    long_count: int
    short_count: int
    flat_count: int
    hold_count: int

    def __post_init__(self) -> None:
        require_utc(self.decision_timestamp)
        if self.asset_class is not self.instrument.asset_class:
            raise ValueError("unified intent asset class differs from instrument")
        if not all(
            value.strip()
            for value in (
                self.intent_id,
                self.policy_id,
                self.policy_version,
                self.configuration_id,
            )
        ):
            raise ValueError("unified intent identity fields must not be blank")
        if any(count < 0 for count in self.counts):
            raise ValueError("unified intent counts must be non-negative")
        if self.direction is FusionDirection.UNAVAILABLE and self.status not in {
            FusionStatus.REGIME_UNAVAILABLE,
            FusionStatus.ALL_SIGNALS_UNAVAILABLE,
        }:
            raise ValueError("unavailable direction requires an unavailable status")

    @property
    def counts(self) -> tuple[int, int, int, int]:
        """Return directional and non-directional participation counts."""

        return self.long_count, self.short_count, self.flat_count, self.hold_count


def configuration_identity(
    policy_id: str,
    policy_version: str,
    parameters: Mapping[str, int | float | str],
    compatibility_rules: tuple[RegimeCompatibilityRule, ...],
) -> str:
    """Hash immutable policy configuration without reference to returns or P&L."""

    payload = {
        "policy_id": policy_id,
        "policy_version": policy_version,
        "parameters": dict(sorted(parameters.items())),
        "compatibility_rules": sorted(
            (rule.canonical() for rule in compatibility_rules),
            key=lambda rule: json.dumps(rule, sort_keys=True, separators=(",", ":")),
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def deterministic_intent_id(
    *,
    context: FusionContext,
    policy: FusionPolicy,
    decision: PolicyDecision,
    eligible: tuple[NormalizedSignal, ...],
    excluded: tuple[SignalExclusion, ...],
) -> str:
    """Create a stable, order-independent identity for one fused decision."""

    payload = {
        "symbol": context.instrument.canonical_symbol,
        "timeframe": context.timeframe.value,
        "decision_timestamp": context.decision_timestamp.isoformat(),
        "policy_id": policy.policy_id,
        "policy_version": policy.policy_version,
        "configuration_id": policy.configuration_id,
        "regime_state_id": context.regime_state.state_id if context.regime_state else None,
        "direction": decision.direction.value,
        "status": decision.status.value,
        "eligible": [signal.source_signal_id for signal in eligible],
        "excluded": [
            (item.signal.source_signal_id, item.eligibility.value, item.reason) for item in excluded
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"{policy.policy_id}-{hashlib.sha256(encoded).hexdigest()[:16]}"


__all__ = [
    "EligibilityState",
    "FusionContext",
    "FusionDirection",
    "FusionPolicy",
    "FusionPolicyMetadata",
    "FusionStatus",
    "NormalizedSignal",
    "PolicyDecision",
    "RegimeCompatibilityRule",
    "SignalExclusion",
    "UnifiedTradeIntent",
    "configuration_identity",
    "deterministic_intent_id",
]
