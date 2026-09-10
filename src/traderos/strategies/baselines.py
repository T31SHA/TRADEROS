"""Four transparent, deterministic baseline strategies."""

from decimal import Decimal
from math import isfinite
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from traderos.data.instruments import AssetClass
from traderos.data.timeframes import Timeframe
from traderos.strategies.base import RuleStrategy
from traderos.strategies.models import (
    FeatureRequirement,
    SignalDirection,
    StrategyContext,
    StrategyMetadata,
    StrategyResult,
)

FOREX_TIMEFRAMES: Final[frozenset[Timeframe]] = frozenset(
    {Timeframe.M15, Timeframe.H1, Timeframe.H4, Timeframe.D1}
)
EQUITY_TIMEFRAMES: Final[frozenset[Timeframe]] = frozenset({Timeframe.H1, Timeframe.D1})


class _Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    target_quantity: Decimal = Field(default=Decimal("1"), gt=0)

    @field_validator("target_quantity")
    @classmethod
    def finite_quantity(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("target_quantity must be finite and positive")
        return value


class TrendParameters(_Parameters):
    fast_period: int = Field(default=10, gt=0)
    slow_period: int = Field(default=30, gt=0)
    trend_strength_threshold: float = Field(default=0.0, ge=0.0)

    @field_validator("trend_strength_threshold")
    @classmethod
    def finite_threshold(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError("trend_strength_threshold must be finite")
        return value

    @model_validator(mode="after")
    def fast_precedes_slow(self) -> "TrendParameters":
        if self.fast_period >= self.slow_period:
            raise ValueError("fast_period must be less than slow_period")
        return self


class BreakoutParameters(_Parameters):
    lookback: int = Field(default=20, gt=0)
    breakout_buffer: float = Field(default=0.0, ge=0.0)

    @field_validator("breakout_buffer")
    @classmethod
    def finite_buffer(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError("breakout_buffer must be finite")
        return value


class MomentumParameters(_Parameters):
    lookback: int = Field(default=20, gt=0)
    minimum_momentum: float = 0.0

    @field_validator("minimum_momentum")
    @classmethod
    def finite_momentum(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError("minimum_momentum must be finite")
        return value


def _parameter_mapping(parameters: BaseModel) -> dict[str, int | float | str | Decimal]:
    return dict(parameters.model_dump())


class ForexTrendFollowingStrategy(RuleStrategy):
    strategy_id = "forex_trend_following"
    strategy_version = "1"

    def __init__(self, parameters: TrendParameters | None = None) -> None:
        self.config = parameters or TrendParameters()

    @property
    def parameters(self) -> dict[str, int | float | str | Decimal]:
        return _parameter_mapping(self.config)

    @property
    def metadata(self) -> StrategyMetadata:
        requirements = (
            FeatureRequirement.from_parameters("ema", 1, {"window": self.config.fast_period}),
            FeatureRequirement.from_parameters("ema", 1, {"window": self.config.slow_period}),
        )
        return StrategyMetadata(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            supported_asset_classes=frozenset({AssetClass.FOREX}),
            supported_timeframes=FOREX_TIMEFRAMES,
            required_features=requirements,
            warmup_bars=self.config.slow_period - 1,
            parameter_schema=tuple(self.parameters),
            description="Causal fast/slow EMA trend-following baseline.",
        )

    def on_bar(self, context: StrategyContext) -> StrategyResult:
        self._validate_context(context)
        requirements = self.metadata.required_features
        fast = context.feature_value(requirements[0])
        slow = context.feature_value(requirements[1])
        if fast is None or slow is None:
            return self._result(
                context,
                SignalDirection.HOLD,
                "warmup",
                requirements,
                target_quantity=self.config.target_quantity,
            )
        strength = (fast - slow) / abs(slow) if slow else 0.0
        if abs(strength) < self.config.trend_strength_threshold:
            direction = SignalDirection.FLAT
            reason = "trend_below_threshold"
        elif fast > slow:
            direction = SignalDirection.LONG
            reason = "fast_ema_above_slow_ema"
        elif fast < slow:
            direction = SignalDirection.SHORT
            reason = "fast_ema_below_slow_ema"
        else:
            direction = SignalDirection.FLAT
            reason = "ema_neutral"
        return self._result(
            context,
            direction,
            reason,
            requirements,
            score=strength,
            target_quantity=self.config.target_quantity,
        )


class ForexBreakoutStrategy(RuleStrategy):
    strategy_id = "forex_breakout"
    strategy_version = "1"

    def __init__(self, parameters: BreakoutParameters | None = None) -> None:
        self.config = parameters or BreakoutParameters()

    @property
    def parameters(self) -> dict[str, int | float | str | Decimal]:
        return _parameter_mapping(self.config)

    @property
    def metadata(self) -> StrategyMetadata:
        requirements = (
            FeatureRequirement.from_parameters(
                "distance_to_previous_high", 1, {"window": self.config.lookback}
            ),
            FeatureRequirement.from_parameters(
                "distance_to_previous_low", 1, {"window": self.config.lookback}
            ),
        )
        return StrategyMetadata(
            self.strategy_id,
            self.strategy_version,
            frozenset({AssetClass.FOREX}),
            FOREX_TIMEFRAMES,
            requirements,
            self.config.lookback,
            tuple(self.parameters),
            "Previous-window Forex breakout baseline.",
        )

    def on_bar(self, context: StrategyContext) -> StrategyResult:
        self._validate_context(context)
        requirements = self.metadata.required_features
        above = context.feature_value(requirements[0])
        below = context.feature_value(requirements[1])
        if above is None or below is None:
            return self._result(
                context,
                SignalDirection.HOLD,
                "warmup",
                requirements,
                target_quantity=self.config.target_quantity,
            )
        if above > self.config.breakout_buffer:
            direction, reason = SignalDirection.LONG, "close_above_previous_high"
            score = above
        elif below < -self.config.breakout_buffer:
            direction, reason = SignalDirection.SHORT, "close_below_previous_low"
            score = below
        else:
            direction, reason, score = SignalDirection.FLAT, "inside_previous_range", 0.0
        return self._result(
            context,
            direction,
            reason,
            requirements,
            score=score,
            target_quantity=self.config.target_quantity,
        )


class EquityMomentumStrategy(RuleStrategy):
    strategy_id = "equity_momentum"
    strategy_version = "1"

    def __init__(self, parameters: MomentumParameters | None = None) -> None:
        self.config = parameters or MomentumParameters()

    @property
    def parameters(self) -> dict[str, int | float | str | Decimal]:
        return _parameter_mapping(self.config)

    @property
    def metadata(self) -> StrategyMetadata:
        requirement = FeatureRequirement.from_parameters(
            "rolling_return", 1, {"window": self.config.lookback}
        )
        return StrategyMetadata(
            self.strategy_id,
            self.strategy_version,
            frozenset({AssetClass.EQUITY, AssetClass.ETF}),
            EQUITY_TIMEFRAMES,
            (requirement,),
            self.config.lookback,
            tuple(self.parameters),
            "Long-only causal equity momentum baseline.",
        )

    def on_bar(self, context: StrategyContext) -> StrategyResult:
        self._validate_context(context)
        requirement = self.metadata.required_features[0]
        momentum = context.feature_value(requirement)
        if momentum is None:
            return self._result(
                context,
                SignalDirection.HOLD,
                "warmup",
                (requirement,),
                target_quantity=self.config.target_quantity,
            )
        direction = (
            SignalDirection.LONG
            if momentum > self.config.minimum_momentum
            else SignalDirection.FLAT
        )
        reason = (
            "momentum_above_threshold"
            if direction is SignalDirection.LONG
            else "momentum_below_threshold"
        )
        return self._result(
            context,
            direction,
            reason,
            (requirement,),
            score=momentum,
            target_quantity=self.config.target_quantity,
        )


class EquityBreakoutStrategy(RuleStrategy):
    strategy_id = "equity_breakout"
    strategy_version = "1"

    def __init__(self, parameters: BreakoutParameters | None = None) -> None:
        self.config = parameters or BreakoutParameters()

    @property
    def parameters(self) -> dict[str, int | float | str | Decimal]:
        return _parameter_mapping(self.config)

    @property
    def metadata(self) -> StrategyMetadata:
        requirement = FeatureRequirement.from_parameters(
            "distance_to_previous_high", 1, {"window": self.config.lookback}
        )
        return StrategyMetadata(
            self.strategy_id,
            self.strategy_version,
            frozenset({AssetClass.EQUITY, AssetClass.ETF}),
            EQUITY_TIMEFRAMES,
            (requirement,),
            self.config.lookback,
            tuple(self.parameters),
            "Long-only previous-window equity breakout baseline.",
        )

    def on_bar(self, context: StrategyContext) -> StrategyResult:
        self._validate_context(context)
        requirement = self.metadata.required_features[0]
        distance = context.feature_value(requirement)
        if distance is None:
            return self._result(
                context,
                SignalDirection.HOLD,
                "warmup",
                (requirement,),
                target_quantity=self.config.target_quantity,
            )
        direction = (
            SignalDirection.LONG if distance > self.config.breakout_buffer else SignalDirection.FLAT
        )
        reason = (
            "close_above_previous_high"
            if direction is SignalDirection.LONG
            else "inside_previous_high"
        )
        return self._result(
            context,
            direction,
            reason,
            (requirement,),
            score=distance,
            target_quantity=self.config.target_quantity,
        )


__all__ = [
    "BreakoutParameters",
    "EquityBreakoutStrategy",
    "EquityMomentumStrategy",
    "ForexBreakoutStrategy",
    "ForexTrendFollowingStrategy",
    "MomentumParameters",
    "TrendParameters",
]
