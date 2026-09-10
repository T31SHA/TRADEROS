"""Versioned, timestamp-aware feature contracts.

The Phase 1 bar timestamp identifies the opening instant of a bar.  A feature
that uses the completed current bar is therefore available at
``bar.timestamp + timeframe.duration`` plus its declared publication lag.
"""

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import isfinite

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from traderos.data.bars import MarketBar
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.lineage import AdjustmentPolicy
from traderos.data.time import require_utc
from traderos.data.timeframes import Timeframe, require_supported_timeframe


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CausalSafety(StrEnum):
    """Causality classification for a registered definition."""

    CAUSAL = "causal"


class MissingDataPolicy(StrEnum):
    """How an expected but unavailable feature input is represented."""

    WARMUP_NULL = "warmup_null"
    EXPLICIT_NULL = "explicit_null"


class FeatureStatus(StrEnum):
    """Reason a feature observation has or does not have a numeric value."""

    VALUE = "value"
    WARMUP = "warmup"
    MISSING_INPUT = "missing_input"
    UNDEFINED = "undefined"


class FeatureDefinition(BaseModel):
    """Controlled metadata for one immutable feature version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    version: int = Field(ge=1)
    description: str = Field(min_length=1)
    applicable_asset_classes: frozenset[AssetClass]
    applicable_timeframes: frozenset[Timeframe]
    required_columns: tuple[str, ...] = Field(min_length=1)
    dependencies: tuple[str, ...] = ()
    lookback: int = Field(ge=1)
    computation: str = Field(min_length=1)
    default_parameters: dict[str, int | float | str] = Field(default_factory=dict)
    parameter_names: frozenset[str] = frozenset()
    expected_output_type: str = "float"
    availability_lag: timedelta = timedelta(0)
    causal_safety: CausalSafety = CausalSafety.CAUSAL
    missing_data_policy: MissingDataPolicy = MissingDataPolicy.WARMUP_NULL
    nullable: bool = True
    source: str = "canonical_market_data"
    implementation_version: str = "1"

    @field_validator("dependencies", "required_columns")
    @classmethod
    def reject_blank_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("feature names and columns must not be blank")
        return values

    @field_validator("availability_lag")
    @classmethod
    def reject_negative_lag(cls, value: timedelta) -> timedelta:
        if value < timedelta(0):
            raise ValueError("availability_lag must not be negative")
        return value

    @model_validator(mode="after")
    def validate_scope(self) -> "FeatureDefinition":
        if not self.applicable_asset_classes:
            raise ValueError("at least one asset class is required")
        if not self.applicable_timeframes:
            raise ValueError("at least one timeframe is required")
        unknown_defaults = set(self.default_parameters) - self.parameter_names
        if unknown_defaults:
            raise ValueError(f"default parameters are not declared: {sorted(unknown_defaults)}")
        return self

    @property
    def key(self) -> tuple[str, int]:
        return self.name, self.version


FeatureMetadata = FeatureDefinition


class FeatureContext(BaseModel):
    """Immutable computation context shared by all requested features."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument: Instrument
    timeframe: Timeframe
    source_dataset_version: str = Field(min_length=1)
    adjustment_policy: AdjustmentPolicy = AdjustmentPolicy.RAW
    start: datetime | None = None
    end: datetime | None = None
    decision_lag: timedelta = timedelta(0)
    computation_timestamp: datetime = Field(default_factory=_utc_now)

    @field_validator("start", "end", "computation_timestamp")
    @classmethod
    def require_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            require_utc(value)
        return value

    @field_validator("decision_lag")
    @classmethod
    def require_nonnegative_decision_lag(cls, value: timedelta) -> timedelta:
        if value < timedelta(0):
            raise ValueError("decision_lag must not be negative")
        return value

    @model_validator(mode="after")
    def validate_context(self) -> "FeatureContext":
        require_supported_timeframe(self.instrument.asset_class, self.timeframe)
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError("start must not be after end")
        return self


class FeatureRequest(BaseModel):
    """A safe, data-only feature request; it cannot execute arbitrary code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    version: int = Field(ge=1)
    parameters: dict[str, int | float | str] = Field(default_factory=dict)


class FeatureLineage(BaseModel):
    """Provenance required to reproduce a feature observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_dataset_version: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    asset_class: AssetClass
    timeframe: Timeframe
    adjustment_policy: AdjustmentPolicy
    feature_name: str = Field(min_length=1)
    feature_version: int = Field(ge=1)
    parameters: dict[str, int | float | str] = Field(default_factory=dict)
    computation_version: str = Field(min_length=1)
    input_columns: tuple[str, ...] = Field(min_length=1)
    dependencies: tuple[str, ...] = ()
    computed_at: datetime

    @field_validator("computed_at")
    @classmethod
    def computed_at_must_be_utc(cls, value: datetime) -> datetime:
        require_utc(value)
        return value


class FeatureObservation(BaseModel):
    """One timestamped feature value and its causal availability contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument: Instrument
    timeframe: Timeframe
    observation_timestamp: datetime
    availability_timestamp: datetime
    decision_timestamp: datetime
    feature_name: str = Field(min_length=1)
    feature_version: int = Field(ge=1)
    value: float | None = None
    status: FeatureStatus
    lineage: FeatureLineage

    @field_validator("observation_timestamp", "availability_timestamp", "decision_timestamp")
    @classmethod
    def timestamps_must_be_utc(cls, value: datetime) -> datetime:
        require_utc(value)
        return value

    @field_validator("value")
    @classmethod
    def value_must_be_finite(cls, value: float | None) -> float | None:
        if value is not None and not isfinite(value):
            raise ValueError("feature values must be finite")
        return value

    @model_validator(mode="after")
    def validate_causal_order(self) -> "FeatureObservation":
        if self.availability_timestamp < self.observation_timestamp:
            raise ValueError("availability cannot precede observation")
        if self.decision_timestamp < self.availability_timestamp:
            raise ValueError("decision cannot precede availability")
        if self.status is FeatureStatus.VALUE and self.value is None:
            raise ValueError("value status requires a numeric value")
        if self.status is not FeatureStatus.VALUE and self.value is not None:
            raise ValueError("non-value status must not contain a numeric value")
        if self.lineage.feature_name != self.feature_name:
            raise ValueError("feature lineage name does not match observation")
        if self.lineage.feature_version != self.feature_version:
            raise ValueError("feature lineage version does not match observation")
        if self.lineage.symbol != self.instrument.canonical_symbol:
            raise ValueError("feature lineage symbol does not match observation")
        return self


class FeatureSet(BaseModel):
    """A validated long-form collection of feature observations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    context: FeatureContext
    observations: tuple[FeatureObservation, ...]

    @property
    def feature_keys(self) -> frozenset[tuple[str, int]]:
        return frozenset(
            (observation.feature_name, observation.feature_version)
            for observation in self.observations
        )

    def values_by_timestamp(
        self,
    ) -> dict[datetime, dict[tuple[str, int], float | None]]:
        """Return a convenient wide view without changing storage semantics."""

        result: dict[datetime, dict[tuple[str, int], float | None]] = {}
        for observation in self.observations:
            result.setdefault(observation.observation_timestamp, {})[
                (observation.feature_name, observation.feature_version)
            ] = observation.value
        return result


def availability_timestamp(
    bar: MarketBar, definition: FeatureDefinition, decision_lag: timedelta
) -> tuple[datetime, datetime]:
    """Calculate safe availability and decision timestamps for a bar."""

    available = bar.timestamp + bar.timeframe.duration + definition.availability_lag
    return available, available + decision_lag


__all__ = [
    "CausalSafety",
    "FeatureContext",
    "FeatureDefinition",
    "FeatureLineage",
    "FeatureMetadata",
    "FeatureObservation",
    "FeatureRequest",
    "FeatureSet",
    "FeatureStatus",
    "MissingDataPolicy",
    "availability_timestamp",
]
