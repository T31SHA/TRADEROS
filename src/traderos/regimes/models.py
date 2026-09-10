"""Immutable, causal, and versioned market-regime contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import Protocol

from traderos.data.bars import MarketBar
from traderos.data.calendars import MarketCalendar
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.quality import DataQualityEvent
from traderos.data.time import require_utc
from traderos.data.timeframes import Timeframe
from traderos.features.models import FeatureObservation
from traderos.regimes.errors import RegimeCausalityError


class TrendRegime(StrEnum):
    """Directional state from a deterministic trend rule."""

    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    NEUTRAL = "neutral"
    UNAVAILABLE = "unavailable"


class VolatilityRegime(StrEnum):
    """Relative current volatility state from a causal percentile."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    UNAVAILABLE = "unavailable"


class LiquidityRegime(StrEnum):
    """Tradability state supported by actually available market evidence."""

    NORMAL = "normal"
    STRESSED = "stressed"
    UNKNOWN = "unknown"


class DataSessionState(StrEnum):
    """Calendar and data-availability state, separate from market regimes."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    WARMUP = "warmup"
    DATA_UNAVAILABLE = "data_unavailable"


@dataclass(frozen=True)
class FeatureRequirement:
    """One exact Phase 2 feature identity required by a detector."""

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

    def matches(self, observation: FeatureObservation) -> bool:
        return (
            observation.feature_name == self.name
            and observation.feature_version == self.version
            and tuple(sorted(observation.lineage.parameters.items())) == self.parameters
        )


@dataclass(frozen=True)
class FeatureProvenance:
    """Compact reference to a causal feature used in one regime state."""

    requirement: FeatureRequirement
    observation_timestamp: datetime
    availability_timestamp: datetime

    def __post_init__(self) -> None:
        require_utc(self.observation_timestamp)
        require_utc(self.availability_timestamp)
        if self.availability_timestamp < self.observation_timestamp:
            raise ValueError("feature provenance availability precedes observation")


@dataclass(frozen=True)
class RegimeMetadata:
    """Versioned detector declaration and supported causal dependencies."""

    regime_detector_id: str
    regime_detector_version: str
    supported_asset_classes: frozenset[AssetClass]
    supported_timeframes: frozenset[Timeframe]
    required_features: tuple[FeatureRequirement, ...]
    parameter_schema: tuple[str, ...]
    description: str

    def __post_init__(self) -> None:
        if not self.regime_detector_id.strip() or not self.regime_detector_version.strip():
            raise ValueError("regime detector identity must not be blank")
        if not self.supported_asset_classes or not self.supported_timeframes:
            raise ValueError("regime detector scope must not be empty")
        if len(set(self.parameter_schema)) != len(self.parameter_schema):
            raise ValueError("regime parameter schema names must be unique")
        if not self.description.strip():
            raise ValueError("regime detector description must not be blank")


@dataclass(frozen=True)
class RegimeState:
    """Auditable classification of one instrument at one decision timestamp."""

    state_id: str
    instrument: Instrument
    asset_class: AssetClass
    timeframe: Timeframe
    decision_timestamp: datetime
    regime_detector_id: str
    regime_detector_version: str
    configuration_id: str
    trend: TrendRegime
    volatility: VolatilityRegime
    liquidity: LiquidityRegime
    data_session: DataSessionState
    feature_provenance: tuple[FeatureProvenance, ...]
    market_data_provenance: tuple[str, ...] = ()
    trend_strength: float | None = None

    def __post_init__(self) -> None:
        require_utc(self.decision_timestamp)
        if self.asset_class is not self.instrument.asset_class:
            raise ValueError("regime state asset class differs from instrument")
        if not all(
            value.strip()
            for value in (
                self.state_id,
                self.regime_detector_id,
                self.regime_detector_version,
                self.configuration_id,
            )
        ):
            raise ValueError("regime state identity fields must not be blank")
        if self.trend_strength is not None and not isfinite(self.trend_strength):
            raise ValueError("trend strength must be finite")


@dataclass(frozen=True)
class RegimeContext:
    """Bounded immutable information visible to a detector at one decision."""

    bar: MarketBar
    decision_timestamp: datetime
    feature_observations: tuple[FeatureObservation, ...]
    calendar: MarketCalendar
    detector_parameters: Mapping[str, int | float | str]
    quality_events: tuple[DataQualityEvent, ...] = ()
    prior_state: RegimeState | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "detector_parameters", MappingProxyType(dict(self.detector_parameters))
        )
        require_utc(self.decision_timestamp)
        if self.decision_timestamp < self.bar.timestamp + self.bar.timeframe.duration:
            raise RegimeCausalityError("regime context precedes current bar completion")
        for observation in self.feature_observations:
            if observation.instrument != self.bar.instrument:
                raise RegimeCausalityError("feature observation crosses instruments")
            if observation.timeframe is not self.bar.timeframe:
                raise RegimeCausalityError("feature observation crosses timeframes")
            if observation.observation_timestamp > self.bar.timestamp:
                raise RegimeCausalityError("future feature observation entered context")
            if observation.availability_timestamp > self.decision_timestamp:
                raise RegimeCausalityError("unavailable feature observation entered context")
        for event in self.quality_events:
            if event.occurred_at > self.decision_timestamp:
                raise RegimeCausalityError("future quality event entered context")
            if event.bar_timestamp is not None and event.bar_timestamp > self.bar.timestamp:
                raise RegimeCausalityError("future quality-bar event entered context")
            if event.symbol is not None and event.symbol != self.bar.symbol:
                raise RegimeCausalityError("quality event crosses instruments")
            if event.timeframe is not None and event.timeframe != self.bar.timeframe.value:
                raise RegimeCausalityError("quality event crosses timeframes")
        if self.prior_state is not None:
            if self.prior_state.instrument != self.bar.instrument:
                raise RegimeCausalityError("prior regime state crosses instruments")
            if self.prior_state.timeframe is not self.bar.timeframe:
                raise RegimeCausalityError("prior regime state crosses timeframes")
            if self.prior_state.decision_timestamp >= self.decision_timestamp:
                raise RegimeCausalityError("prior regime state is not historical")

    @property
    def instrument(self) -> Instrument:
        return self.bar.instrument

    @property
    def asset_class(self) -> AssetClass:
        return self.bar.asset_class

    @property
    def timeframe(self) -> Timeframe:
        return self.bar.timeframe

    def current_feature(self, requirement: FeatureRequirement) -> FeatureObservation | None:
        """Return the exact feature for this completed bar, never a stale value."""

        matches = [
            observation
            for observation in self.feature_observations
            if observation.observation_timestamp == self.bar.timestamp
            and requirement.matches(observation)
        ]
        if not matches:
            return None
        if len(matches) > 1:
            raise RegimeCausalityError("duplicate required feature observations entered context")
        return matches[0]


class RegimeDetector(Protocol):
    """Observation-only detector contract for a declared regime version."""

    regime_detector_id: str
    regime_detector_version: str

    @property
    def metadata(self) -> RegimeMetadata:
        """Return immutable identity, scope, dependency, and parameter metadata."""

    @property
    def parameters(self) -> Mapping[str, int | float | str]:
        """Return validated immutable detector parameters."""

    def evaluate(self, context: RegimeContext) -> RegimeState:
        """Classify a bounded causal context without creating orders or signals."""


def configuration_identity(
    regime_detector_id: str,
    regime_detector_version: str,
    parameters: Mapping[str, int | float | str],
) -> str:
    """Hash stable detector identity and effective parameters."""

    payload = {
        "regime_detector_id": regime_detector_id,
        "regime_detector_version": regime_detector_version,
        "parameters": dict(sorted(parameters.items())),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def deterministic_state_id(
    *,
    instrument: Instrument,
    timeframe: Timeframe,
    decision_timestamp: datetime,
    regime_detector_id: str,
    regime_detector_version: str,
    configuration_id: str,
    trend: TrendRegime,
    volatility: VolatilityRegime,
    liquidity: LiquidityRegime,
    data_session: DataSessionState,
) -> str:
    """Create a stable state identifier without wall-clock or mutable state."""

    payload = {
        "symbol": instrument.canonical_symbol,
        "timeframe": timeframe.value,
        "decision_timestamp": require_utc(decision_timestamp).isoformat(),
        "regime_detector_id": regime_detector_id,
        "regime_detector_version": regime_detector_version,
        "configuration_id": configuration_id,
        "trend": trend.value,
        "volatility": volatility.value,
        "liquidity": liquidity.value,
        "data_session": data_session.value,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"{regime_detector_id}-{hashlib.sha256(encoded).hexdigest()[:16]}"


__all__ = [
    "DataSessionState",
    "FeatureProvenance",
    "FeatureRequirement",
    "LiquidityRegime",
    "RegimeContext",
    "RegimeDetector",
    "RegimeMetadata",
    "RegimeState",
    "TrendRegime",
    "VolatilityRegime",
    "configuration_identity",
    "deterministic_state_id",
]
