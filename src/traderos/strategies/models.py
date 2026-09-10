"""Immutable strategy metadata, decision-time context, signals, and intents."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from math import isfinite
from types import MappingProxyType

from traderos.backtesting.models import OrderSide, OrderType, TimeInForce
from traderos.data.bars import MarketBar
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.time import require_utc
from traderos.data.timeframes import Timeframe
from traderos.features.models import FeatureObservation, FeatureStatus
from traderos.strategies.errors import MissingFeatureError, StrategyCausalityError


class SignalDirection(StrEnum):
    """Semantic strategy direction, distinct from an executable order side."""

    LONG = "long"
    SHORT = "short"
    FLAT = "flat"
    HOLD = "hold"


@dataclass(frozen=True)
class FeatureRequirement:
    """One exact version/parameterization required by a strategy."""

    name: str
    version: int
    parameters: tuple[tuple[str, int | float | str], ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip() or self.version < 1:
            raise ValueError("feature requirement name and version are invalid")
        names = [name for name, _ in self.parameters]
        if any(not name.strip() for name in names) or len(set(names)) != len(names):
            raise ValueError("feature requirement parameters must be unique and non-blank")

    @classmethod
    def from_parameters(
        cls,
        name: str,
        version: int,
        parameters: Mapping[str, int | float | str],
    ) -> FeatureRequirement:
        return cls(name, version, tuple(sorted(parameters.items())))

    @property
    def parameter_mapping(self) -> Mapping[str, int | float | str]:
        return MappingProxyType(dict(self.parameters))

    def matches(self, observation: FeatureObservation) -> bool:
        actual = tuple(sorted(observation.lineage.parameters.items()))
        return (
            observation.feature_name == self.name
            and observation.feature_version == self.version
            and actual == self.parameters
        )


@dataclass(frozen=True)
class StrategyMetadata:
    """Versioned scope and dependency declaration for one strategy."""

    strategy_id: str
    strategy_version: str
    supported_asset_classes: frozenset[AssetClass]
    supported_timeframes: frozenset[Timeframe]
    required_features: tuple[FeatureRequirement, ...]
    warmup_bars: int
    parameter_schema: tuple[str, ...]
    description: str

    def __post_init__(self) -> None:
        if not self.strategy_id.strip() or not self.strategy_version.strip():
            raise ValueError("strategy identity must not be blank")
        if not self.supported_asset_classes or not self.supported_timeframes:
            raise ValueError("strategy scope must not be empty")
        if self.warmup_bars < 0:
            raise ValueError("warmup_bars must be non-negative")
        if not self.description.strip():
            raise ValueError("strategy description must not be blank")
        if len(set(self.parameter_schema)) != len(self.parameter_schema):
            raise ValueError("parameter schema names must be unique")


@dataclass(frozen=True)
class StrategyContext:
    """Bounded immutable information visible at one completed-bar decision."""

    event_timestamp: datetime
    bar: MarketBar
    feature_observations: tuple[FeatureObservation, ...]
    position_quantity: Decimal
    cash: Decimal
    equity: Decimal
    parameters: Mapping[str, int | float | str | Decimal]
    strategy_id: str
    strategy_version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))
        require_utc(self.event_timestamp)
        if self.event_timestamp < self.bar.timestamp + self.bar.timeframe.duration:
            raise StrategyCausalityError("strategy context precedes bar completion")
        if not self.position_quantity.is_finite():
            raise StrategyCausalityError("strategy position is not finite")
        for observation in self.feature_observations:
            if observation.instrument != self.bar.instrument:
                raise StrategyCausalityError("feature observation crosses instruments")
            if observation.timeframe is not self.bar.timeframe:
                raise StrategyCausalityError("feature observation crosses timeframes")
            if observation.observation_timestamp > self.bar.timestamp:
                raise StrategyCausalityError("future feature observation entered context")
            if observation.availability_timestamp > self.event_timestamp:
                raise StrategyCausalityError("unavailable feature entered context")

    @property
    def instrument(self) -> Instrument:
        return self.bar.instrument

    @property
    def asset_class(self) -> AssetClass:
        return self.bar.asset_class

    @property
    def timeframe(self) -> Timeframe:
        return self.bar.timeframe

    def feature_observation(self, requirement: FeatureRequirement) -> FeatureObservation:
        candidates = [
            observation
            for observation in self.feature_observations
            if requirement.matches(observation)
        ]
        if not candidates:
            raise MissingFeatureError(
                f"missing causal feature {requirement.name}.v{requirement.version}"
            )
        return max(candidates, key=lambda item: item.observation_timestamp)

    def feature_value(self, requirement: FeatureRequirement) -> float | None:
        observation = self.feature_observation(requirement)
        return observation.value if observation.status is FeatureStatus.VALUE else None


@dataclass(frozen=True)
class StrategySignal:
    """Auditable deterministic strategy decision."""

    signal_id: str
    strategy_id: str
    strategy_version: str
    instrument: Instrument
    timestamp: datetime
    timeframe: Timeframe
    direction: SignalDirection
    reason: str
    feature_provenance: tuple[FeatureRequirement, ...]
    confidence: float | None = None
    score: float | None = None
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        require_utc(self.timestamp)
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between zero and one")
        for value in (self.confidence, self.score):
            if value is not None and not isfinite(value):
                raise ValueError("signal numeric metadata must be finite")


@dataclass(frozen=True)
class OrderIntent:
    """Strategy request converted to a Phase 3 order by an adapter."""

    instrument: Instrument
    side: OrderSide
    quantity: Decimal
    signal_id: str
    strategy_id: str
    strategy_version: str
    reason: str
    order_type: OrderType = OrderType.MARKET
    time_in_force: TimeInForce = TimeInForce.GTC
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    target_position: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.quantity.is_finite() or self.quantity <= 0:
            raise ValueError("order intent quantity must be finite and positive")
        if self.target_position is not None and not self.target_position.is_finite():
            raise ValueError("target position must be finite")
        if self.order_type is OrderType.LIMIT:
            if (
                self.limit_price is None
                or not self.limit_price.is_finite()
                or self.limit_price <= 0
            ):
                raise ValueError("limit intents require a positive limit price")
            if self.stop_price is not None:
                raise ValueError("limit intents must not include a stop price")
        elif self.order_type is OrderType.STOP:
            if self.stop_price is None or not self.stop_price.is_finite() or self.stop_price <= 0:
                raise ValueError("stop intents require a positive stop price")
            if self.limit_price is not None:
                raise ValueError("stop intents must not include a limit price")
        elif self.limit_price is not None or self.stop_price is not None:
            raise ValueError("market intents must not include conditional prices")


@dataclass(frozen=True)
class StrategyResult:
    """One signal and zero or more order intents."""

    signal: StrategySignal
    order_intents: tuple[OrderIntent, ...] = ()


def deterministic_signal_id(
    strategy_id: str,
    strategy_version: str,
    instrument: Instrument,
    timeframe: Timeframe,
    timestamp: datetime,
    direction: SignalDirection,
) -> str:
    """Create a stable identity without wall-clock or random state."""

    payload = {
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "symbol": instrument.canonical_symbol,
        "timeframe": timeframe.value,
        "timestamp": require_utc(timestamp).isoformat(),
        "direction": direction.value,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"{strategy_id}-{hashlib.sha256(encoded).hexdigest()[:16]}"


__all__ = [
    "FeatureRequirement",
    "OrderIntent",
    "SignalDirection",
    "StrategyContext",
    "StrategyMetadata",
    "StrategyResult",
    "StrategySignal",
    "deterministic_signal_id",
]
