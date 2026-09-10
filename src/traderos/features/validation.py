"""Input and output integrity checks for the feature boundary."""

from collections import defaultdict
from collections.abc import Sequence
from math import isfinite

from traderos.data.bars import MarketBar
from traderos.data.errors import TimestampNormalizationError
from traderos.data.time import require_utc
from traderos.features.errors import FeatureValidationError
from traderos.features.models import FeatureContext, FeatureObservation, FeatureSet, FeatureStatus


def validate_bar_series(bars: Sequence[MarketBar], context: FeatureContext) -> None:
    """Reject unsafe series before any numerical calculation begins."""

    if not bars:
        raise FeatureValidationError("at least one validated market bar is required")
    previous = None
    for bar in bars:
        try:
            require_utc(bar.timestamp)
        except TimestampNormalizationError as exc:
            raise FeatureValidationError(str(exc)) from exc
        if bar.instrument != context.instrument:
            raise FeatureValidationError("all bars must belong to the context instrument")
        if bar.timeframe is not context.timeframe:
            raise FeatureValidationError("all bars must use the context timeframe")
        if bar.adjustment_policy is not context.adjustment_policy:
            raise FeatureValidationError("raw and adjusted bars cannot be mixed")
        prices = (bar.open, bar.high, bar.low, bar.close)
        if any(not price.is_finite() or price <= 0 for price in prices):
            raise FeatureValidationError("feature inputs require finite positive OHLC prices")
        if bar.high < max(bar.open, bar.close, bar.low) or bar.low > min(
            bar.open, bar.close, bar.high
        ):
            raise FeatureValidationError("feature inputs contain impossible OHLC values")
        if bar.volume is not None and (not bar.volume.is_finite() or bar.volume < 0):
            raise FeatureValidationError("feature inputs require non-negative finite volume")
        if previous is not None and bar.timestamp <= previous:
            raise FeatureValidationError("bars must be strictly ordered with no duplicates")
        previous = bar.timestamp


def validate_feature_set(feature_set: FeatureSet) -> FeatureSet:
    """Validate long-form observations while allowing expected warm-up nulls."""

    seen: set[tuple[str, int, object]] = set()
    by_feature: dict[tuple[str, int], list[FeatureObservation]] = defaultdict(list)
    for observation in feature_set.observations:
        key = (
            observation.feature_name,
            observation.feature_version,
            observation.observation_timestamp,
        )
        if key in seen:
            raise FeatureValidationError(f"duplicate feature observation: {key!r}")
        seen.add(key)
        if observation.instrument != feature_set.context.instrument:
            raise FeatureValidationError("observation instrument differs from feature context")
        if observation.timeframe is not feature_set.context.timeframe:
            raise FeatureValidationError("observation timeframe differs from feature context")
        if observation.lineage.timeframe is not observation.timeframe:
            raise FeatureValidationError("observation lineage timeframe differs")
        if observation.lineage.asset_class is not observation.instrument.asset_class:
            raise FeatureValidationError("observation lineage asset class differs")
        if observation.lineage.source_dataset_version != feature_set.context.source_dataset_version:
            raise FeatureValidationError("observation lineage dataset differs from context")
        if observation.lineage.adjustment_policy is not feature_set.context.adjustment_policy:
            raise FeatureValidationError("observation lineage adjustment policy differs")
        if observation.status is FeatureStatus.VALUE:
            if observation.value is None or not isfinite(observation.value):
                raise FeatureValidationError("value observations must contain finite numbers")
        elif observation.value is not None:
            raise FeatureValidationError("null-status observations must not contain values")
        by_feature[(observation.feature_name, observation.feature_version)].append(observation)

    for feature_key, observations in by_feature.items():
        timestamps = [observation.observation_timestamp for observation in observations]
        if timestamps != sorted(timestamps):
            raise FeatureValidationError(f"feature observations are unsorted: {feature_key!r}")
    return feature_set


__all__ = ["validate_bar_series", "validate_feature_set"]
