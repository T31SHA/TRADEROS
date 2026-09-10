"""Transparent, stateless baseline market-regime detector."""

from math import isfinite
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from traderos.data.instruments import AssetClass
from traderos.data.quality import QualitySeverity
from traderos.data.timeframes import Timeframe
from traderos.features.models import FeatureStatus
from traderos.regimes.errors import RegimeConfigurationError
from traderos.regimes.models import (
    DataSessionState,
    FeatureProvenance,
    FeatureRequirement,
    LiquidityRegime,
    RegimeContext,
    RegimeMetadata,
    RegimeState,
    TrendRegime,
    VolatilityRegime,
    configuration_identity,
    deterministic_state_id,
)

ALL_ASSET_CLASSES: Final[frozenset[AssetClass]] = frozenset(AssetClass)
ALL_TIMEFRAMES: Final[frozenset[Timeframe]] = frozenset(Timeframe)


class BaselineRegimeParameters(BaseModel):
    """Strict, versioned parameters for the Phase 5 baseline detector."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    fast_ema_period: int = Field(default=10, gt=0)
    slow_ema_period: int = Field(default=30, gt=0)
    minimum_trend_separation: float = Field(default=0.001, ge=0.0)
    volatility_percentile_window: int = Field(default=20, gt=0)
    low_volatility_percentile: float = Field(default=0.25, ge=0.0, le=1.0)
    high_volatility_percentile: float = Field(default=0.75, ge=0.0, le=1.0)
    maximum_spread_fraction: float = Field(default=0.002, ge=0.0)
    minimum_equity_volume: float = Field(default=1.0, ge=0.0)

    @field_validator(
        "minimum_trend_separation",
        "low_volatility_percentile",
        "high_volatility_percentile",
        "maximum_spread_fraction",
        "minimum_equity_volume",
    )
    @classmethod
    def finite_float(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError("regime parameters must be finite")
        return value

    @model_validator(mode="after")
    def validate_boundaries(self) -> "BaselineRegimeParameters":
        if self.fast_ema_period >= self.slow_ema_period:
            raise ValueError("fast_ema_period must be less than slow_ema_period")
        if self.low_volatility_percentile >= self.high_volatility_percentile:
            raise ValueError(
                "low_volatility_percentile must be less than high_volatility_percentile"
            )
        return self


class EmaPercentileRegimeDetector:
    """Classify independent trend, volatility, and liquidity dimensions.

    The implementation is intentionally stateless.  It consumes exact current-bar
    Phase 2 observations, so a detector cannot silently substitute stale values
    or examine a future row.  A semantic code change requires a new detector
    version; this baseline is therefore explicitly ``v1``.
    """

    regime_detector_id = "ema_percentile_regime"
    regime_detector_version = "1"

    def __init__(
        self,
        parameters: BaselineRegimeParameters | None = None,
        *,
        supported_asset_classes: frozenset[AssetClass] = ALL_ASSET_CLASSES,
        supported_timeframes: frozenset[Timeframe] = ALL_TIMEFRAMES,
    ) -> None:
        if not supported_asset_classes or not supported_timeframes:
            raise RegimeConfigurationError("detector scope must not be empty")
        self.config = parameters or BaselineRegimeParameters()
        self._supported_asset_classes = supported_asset_classes
        self._supported_timeframes = supported_timeframes

    @property
    def parameters(self) -> dict[str, int | float | str]:
        return dict(self.config.model_dump())

    @property
    def metadata(self) -> RegimeMetadata:
        return RegimeMetadata(
            regime_detector_id=self.regime_detector_id,
            regime_detector_version=self.regime_detector_version,
            supported_asset_classes=self._supported_asset_classes,
            supported_timeframes=self._supported_timeframes,
            required_features=(
                FeatureRequirement.from_parameters(
                    "ema", 1, {"window": self.config.fast_ema_period}
                ),
                FeatureRequirement.from_parameters(
                    "ema", 1, {"window": self.config.slow_ema_period}
                ),
                FeatureRequirement.from_parameters(
                    "volatility_percentile",
                    1,
                    {"window": self.config.volatility_percentile_window},
                ),
            ),
            parameter_schema=tuple(self.parameters),
            description=(
                "Stateless EMA-separation trend, causal volatility-percentile, "
                "and observed-spread tradability detector."
            ),
        )

    @property
    def configuration_id(self) -> str:
        return configuration_identity(
            self.regime_detector_id, self.regime_detector_version, self.parameters
        )

    def _validate_context(self, context: RegimeContext) -> None:
        if context.asset_class not in self.metadata.supported_asset_classes:
            raise RegimeConfigurationError(
                f"{self.regime_detector_id} does not support {context.asset_class.value}"
            )
        if context.timeframe not in self.metadata.supported_timeframes:
            raise RegimeConfigurationError(
                f"{self.regime_detector_id} does not support {context.timeframe.value}"
            )
        if dict(context.detector_parameters) != self.parameters:
            raise RegimeConfigurationError("context detector parameters do not match detector")

    def _state(
        self,
        context: RegimeContext,
        *,
        trend: TrendRegime,
        volatility: VolatilityRegime,
        liquidity: LiquidityRegime,
        data_session: DataSessionState,
        provenance: tuple[FeatureProvenance, ...] = (),
        market_data_provenance: tuple[str, ...] = (),
        trend_strength: float | None = None,
    ) -> RegimeState:
        return RegimeState(
            state_id=deterministic_state_id(
                instrument=context.instrument,
                timeframe=context.timeframe,
                decision_timestamp=context.decision_timestamp,
                regime_detector_id=self.regime_detector_id,
                regime_detector_version=self.regime_detector_version,
                configuration_id=self.configuration_id,
                trend=trend,
                volatility=volatility,
                liquidity=liquidity,
                data_session=data_session,
            ),
            instrument=context.instrument,
            asset_class=context.asset_class,
            timeframe=context.timeframe,
            decision_timestamp=context.decision_timestamp,
            regime_detector_id=self.regime_detector_id,
            regime_detector_version=self.regime_detector_version,
            configuration_id=self.configuration_id,
            trend=trend,
            volatility=volatility,
            liquidity=liquidity,
            data_session=data_session,
            feature_provenance=provenance,
            market_data_provenance=market_data_provenance,
            trend_strength=trend_strength,
        )

    def _liquidity(self, context: RegimeContext) -> tuple[LiquidityRegime, tuple[str, ...]]:
        """Use only an observed quote/spread and, for equities, actual volume."""

        bar = context.bar
        if bar.spread is not None:
            spread = float(bar.spread)
            source = "market_bar:spread"
        elif bar.bid is not None and bar.ask is not None:
            spread = float(bar.ask - bar.bid)
            source = "market_bar:bid_ask"
        else:
            return LiquidityRegime.UNKNOWN, ()
        close = float(bar.close)
        if not isfinite(spread) or not isfinite(close) or close <= 0:
            return LiquidityRegime.UNKNOWN, (source, "market_bar:close")
        if context.asset_class in {AssetClass.EQUITY, AssetClass.ETF}:
            if bar.volume is None or float(bar.volume) < self.config.minimum_equity_volume:
                return LiquidityRegime.UNKNOWN, (source, "market_bar:volume")
            source = f"{source},market_bar:volume"
        regime = (
            LiquidityRegime.STRESSED
            if spread / close > self.config.maximum_spread_fraction
            else LiquidityRegime.NORMAL
        )
        return regime, tuple(source.split(","))

    def evaluate(self, context: RegimeContext) -> RegimeState:
        """Return one immutable observation; this method has no side effects."""

        self._validate_context(context)
        if not context.calendar.is_open_at(context.bar.timestamp):
            return self._state(
                context,
                trend=TrendRegime.UNAVAILABLE,
                volatility=VolatilityRegime.UNAVAILABLE,
                liquidity=LiquidityRegime.UNKNOWN,
                data_session=DataSessionState.INACTIVE,
            )
        if any(event.severity is QualitySeverity.ERROR for event in context.quality_events):
            return self._state(
                context,
                trend=TrendRegime.UNAVAILABLE,
                volatility=VolatilityRegime.UNAVAILABLE,
                liquidity=LiquidityRegime.UNKNOWN,
                data_session=DataSessionState.DATA_UNAVAILABLE,
            )

        observations = tuple(
            (requirement, context.current_feature(requirement))
            for requirement in self.metadata.required_features
        )
        if any(
            observation is None
            or (
                observation.status is not FeatureStatus.VALUE
                and observation.status is not FeatureStatus.WARMUP
            )
            for _, observation in observations
        ):
            return self._state(
                context,
                trend=TrendRegime.UNAVAILABLE,
                volatility=VolatilityRegime.UNAVAILABLE,
                liquidity=LiquidityRegime.UNKNOWN,
                data_session=DataSessionState.DATA_UNAVAILABLE,
            )
        if any(
            observation is not None and observation.status is FeatureStatus.WARMUP
            for _, observation in observations
        ):
            return self._state(
                context,
                trend=TrendRegime.UNAVAILABLE,
                volatility=VolatilityRegime.UNAVAILABLE,
                liquidity=LiquidityRegime.UNKNOWN,
                data_session=DataSessionState.WARMUP,
            )
        resolved = tuple((requirement, observation) for requirement, observation in observations)
        assert all(observation is not None for _, observation in resolved)
        usable = tuple(
            (requirement, observation) for requirement, observation in resolved if observation
        )
        provenance = tuple(
            FeatureProvenance(
                requirement=requirement,
                observation_timestamp=observation.observation_timestamp,
                availability_timestamp=observation.availability_timestamp,
            )
            for requirement, observation in usable
        )
        fast, slow, percentile = (observation.value for _, observation in usable)
        assert fast is not None and slow is not None and percentile is not None
        if (
            not all(isfinite(value) for value in (fast, slow, percentile))
            or slow == 0.0
            or not 0.0 <= percentile <= 1.0
        ):
            return self._state(
                context,
                trend=TrendRegime.UNAVAILABLE,
                volatility=VolatilityRegime.UNAVAILABLE,
                liquidity=LiquidityRegime.UNKNOWN,
                data_session=DataSessionState.DATA_UNAVAILABLE,
                provenance=provenance,
            )
        strength = (fast - slow) / abs(slow)
        if strength > self.config.minimum_trend_separation:
            trend = TrendRegime.TRENDING_UP
        elif strength < -self.config.minimum_trend_separation:
            trend = TrendRegime.TRENDING_DOWN
        else:
            trend = TrendRegime.NEUTRAL
        if percentile < self.config.low_volatility_percentile:
            volatility = VolatilityRegime.LOW
        elif percentile > self.config.high_volatility_percentile:
            volatility = VolatilityRegime.HIGH
        else:
            volatility = VolatilityRegime.NORMAL
        liquidity, market_data_provenance = self._liquidity(context)
        return self._state(
            context,
            trend=trend,
            volatility=volatility,
            liquidity=liquidity,
            data_session=DataSessionState.ACTIVE,
            provenance=provenance,
            market_data_provenance=market_data_provenance,
            trend_strength=strength,
        )


__all__ = ["BaselineRegimeParameters", "EmaPercentileRegimeDetector"]
