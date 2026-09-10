"""Numerical, contract, and leakage tests for the Phase 2 feature engine."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from traderos.data.bars import MarketBar
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.lineage import AdjustmentPolicy
from traderos.data.timeframes import Timeframe
from traderos.features import (
    FeatureComputationError,
    FeatureConfigurationError,
    FeatureContext,
    FeatureDefinition,
    FeatureEngine,
    FeatureObservation,
    FeatureRequest,
    FeatureStatus,
    FeatureValidationError,
    InMemoryFeatureStore,
    build_default_registry,
    compute_feature,
    compute_feature_set,
    validate_bar_series,
    validate_feature_set,
)

BASE = datetime(2024, 1, 2, 0, 0, tzinfo=UTC)


def make_bars(
    closes: list[float],
    *,
    asset_class: AssetClass = AssetClass.FOREX,
    timeframe: Timeframe = Timeframe.H1,
    volume: list[float | None] | None = None,
    adjustment_policy: AdjustmentPolicy = AdjustmentPolicy.RAW,
) -> list[MarketBar]:
    instrument = Instrument(
        canonical_symbol="EUR/USD" if asset_class is AssetClass.FOREX else "AAPL",
        asset_class=asset_class,
        base_currency="EUR" if asset_class is AssetClass.FOREX else None,
        quote_currency="USD",
        trading_currency="USD",
    )
    volumes = volume or [100.0] * len(closes)
    return [
        MarketBar(
            instrument=instrument,
            timeframe=timeframe,
            adjustment_policy=adjustment_policy,
            timestamp=BASE + timeframe.duration * index,
            open=Decimal(str(close - 0.25)),
            high=Decimal(str(close + 0.5)),
            low=Decimal(str(close - 0.5)),
            close=Decimal(str(close)),
            volume=None if volumes[index] is None else Decimal(str(volumes[index])),
            source="fixture",
            currency="USD",
            ingestion_timestamp=BASE,
        )
        for index, close in enumerate(closes)
    ]


def context_for(
    bars: list[MarketBar],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    adjustment_policy: AdjustmentPolicy = AdjustmentPolicy.RAW,
) -> FeatureContext:
    return FeatureContext(
        instrument=bars[0].instrument,
        timeframe=bars[0].timeframe,
        source_dataset_version="fixture-v1",
        adjustment_policy=adjustment_policy,
        start=start,
        end=end,
        computation_timestamp=BASE,
    )


def values(result):
    return [observation.value for observation in result.observations]


def test_registry_contains_controlled_versioned_definitions() -> None:
    registry = build_default_registry()
    assert registry.get("atr", 1).lookback == 14
    assert registry.get("breakout_state", 1).causal_safety.value == "causal"
    with pytest.raises(FeatureConfigurationError):
        registry.register(registry.get("atr", 1))
    with pytest.raises(FeatureConfigurationError):
        registry.get("missing", 1)


def test_all_registered_features_compute_with_explicit_lineage() -> None:
    bars = make_bars([10 + index * 0.25 for index in range(80)])
    engine = FeatureEngine()
    context = context_for(bars)
    requests = [
        FeatureRequest(name=definition.name, version=definition.version)
        for definition in build_default_registry().all()
        if definition.name not in {"equity_volume", "dollar_volume"}
    ]
    result = engine.compute_feature_set(bars, requests, context)
    assert len(result.feature_keys) == len(requests)
    assert all(
        observation.lineage.source_dataset_version == "fixture-v1"
        for observation in result.observations
    )
    assert result.values_by_timestamp()


def test_parameter_validation_and_feature_set_request_validation() -> None:
    bars = make_bars([10 + index for index in range(10)])
    engine = FeatureEngine()
    context = context_for(bars)
    with pytest.raises(FeatureConfigurationError):
        engine.compute_feature_set(bars, [], context)
    with pytest.raises(FeatureConfigurationError):
        engine.compute_feature_set(
            bars,
            [
                FeatureRequest(name="sma", version=1),
                FeatureRequest(name="sma", version=1),
            ],
            context,
        )
    with pytest.raises(FeatureComputationError):
        engine.compute_feature(
            bars,
            FeatureRequest(name="sma", version=1, parameters={"window": 0}),
            context,
        )
    with pytest.raises(FeatureComputationError):
        engine.compute_feature(
            bars,
            FeatureRequest(name="rsi", version=1, parameters={"period": 0}),
            context,
        )
    with pytest.raises(FeatureComputationError):
        engine.compute_feature(
            bars,
            FeatureRequest(name="moving_average_distance", version=1, parameters={"average": "x"}),
            context,
        )
    with pytest.raises(FeatureConfigurationError):
        engine.compute_feature(
            bars,
            FeatureRequest(name="sma", version=1, parameters={"unexpected": 1}),
            context,
        )
    with pytest.raises(FeatureComputationError):
        engine.compute_feature(
            bars,
            FeatureRequest(
                name="volatility_ratio",
                version=1,
                parameters={"short_window": 8, "long_window": 5},
            ),
            context,
        )


def test_zero_range_and_flat_volatility_are_explicitly_undefined() -> None:
    bars = make_bars([10, 10, 10, 10, 10])
    bars = [
        bar.model_copy(
            update={
                "open": Decimal("10"),
                "high": Decimal("10"),
                "low": Decimal("10"),
                "close": Decimal("10"),
            }
        )
        for bar in bars
    ]
    engine = FeatureEngine()
    context = context_for(bars)
    position = engine.compute_feature(
        bars, FeatureRequest(name="range_position", version=1, parameters={"window": 3}), context
    )
    ratio = engine.compute_feature(
        bars,
        FeatureRequest(
            name="volatility_ratio",
            version=1,
            parameters={"short_window": 2, "long_window": 3},
        ),
        context,
    )
    assert position.observations[-1].status is FeatureStatus.UNDEFINED
    assert ratio.observations[-1].status is FeatureStatus.UNDEFINED


def test_context_and_definition_reject_unsafe_metadata() -> None:
    bars = make_bars([1, 2])
    with pytest.raises(ValueError):
        FeatureContext(
            instrument=bars[0].instrument,
            timeframe=Timeframe.H1,
            source_dataset_version="fixture-v1",
            start=BASE + timedelta(hours=2),
            end=BASE,
        )
    with pytest.raises(ValueError):
        FeatureDefinition(
            name="unsafe",
            version=1,
            description="unsafe",
            applicable_asset_classes=frozenset({AssetClass.FOREX}),
            applicable_timeframes=frozenset({Timeframe.H1}),
            required_columns=("close",),
            lookback=1,
            computation="unsafe",
            availability_lag=timedelta(seconds=-1),
        )
    with pytest.raises(FeatureConfigurationError):
        FeatureEngine().compute_feature(
            bars, FeatureRequest(name="missing", version=1), context_for(bars)
        )
    with pytest.raises(ValueError):
        FeatureContext(
            instrument=bars[0].instrument,
            timeframe=Timeframe.H1,
            source_dataset_version="fixture-v1",
            decision_lag=timedelta(seconds=-1),
        )
    with pytest.raises(ValueError):
        FeatureDefinition(
            name="blank_columns",
            version=1,
            description="invalid",
            applicable_asset_classes=frozenset({AssetClass.FOREX}),
            applicable_timeframes=frozenset({Timeframe.H1}),
            required_columns=(" ",),
            lookback=1,
            computation="invalid",
        )
    with pytest.raises(ValueError):
        FeatureDefinition(
            name="no_assets",
            version=1,
            description="invalid",
            applicable_asset_classes=frozenset(),
            applicable_timeframes=frozenset({Timeframe.H1}),
            required_columns=("close",),
            lookback=1,
            computation="invalid",
        )
    with pytest.raises(ValueError):
        FeatureDefinition(
            name="no_timeframes",
            version=1,
            description="invalid",
            applicable_asset_classes=frozenset({AssetClass.FOREX}),
            applicable_timeframes=frozenset(),
            required_columns=("close",),
            lookback=1,
            computation="invalid",
        )


def test_returns_moving_averages_and_warmup_are_deterministic() -> None:
    bars = make_bars([1, 2, 3, 4, 5])
    engine = FeatureEngine()
    context = context_for(bars)

    simple = engine.compute_feature(bars, FeatureRequest(name="simple_return", version=1), context)
    assert values(simple)[:3] == [None, 1.0, 0.5]
    assert values(simple)[3] == pytest.approx(1 / 3)
    assert values(simple)[4] == pytest.approx(0.25)
    assert simple.observations[0].status is FeatureStatus.WARMUP

    sma = engine.compute_feature(
        bars, FeatureRequest(name="sma", version=1, parameters={"window": 3}), context
    )
    ema = engine.compute_feature(
        bars, FeatureRequest(name="ema", version=1, parameters={"window": 3}), context
    )
    assert values(sma) == [None, None, 2.0, 3.0, 4.0]
    assert values(ema) == [None, None, 2.0, 3.0, 4.0]


def test_atr_rsi_and_range_features_use_known_math() -> None:
    bars = make_bars([10, 11, 12, 13, 14])
    engine = FeatureEngine()
    context = context_for(bars)
    atr = engine.compute_feature(
        bars, FeatureRequest(name="atr", version=1, parameters={"window": 3}), context
    )
    rsi = engine.compute_feature(
        bars, FeatureRequest(name="rsi", version=1, parameters={"period": 3}), context
    )
    position = engine.compute_feature(
        bars, FeatureRequest(name="range_position", version=1, parameters={"window": 3}), context
    )
    assert values(atr)[:2] == [None, None]
    assert values(atr)[2] == pytest.approx(4 / 3)
    assert values(rsi)[:3] == [None, None, None]
    assert values(rsi)[3:] == [100.0, 100.0]
    assert all(value is not None and 0 <= value <= 1 for value in values(position)[2:])


def test_previous_window_breakout_does_not_use_current_bar_high() -> None:
    bars = make_bars([10, 11, 12, 13, 14, 15])
    bars[-1] = bars[-1].model_copy(update={"high": Decimal("1000"), "close": Decimal("20")})
    engine = FeatureEngine()
    context = context_for(bars)
    breakout = engine.compute_feature(
        bars, FeatureRequest(name="breakout_state", version=1, parameters={"window": 3}), context
    )
    distance = engine.compute_feature(
        bars,
        FeatureRequest(name="distance_to_previous_high", version=1, parameters={"window": 3}),
        context,
    )
    assert values(breakout)[-1] == 1.0
    assert values(distance)[-1] == pytest.approx(20 / 14.5 - 1)


@pytest.mark.parametrize("name", ["rolling_return", "sma", "rolling_volatility", "atr", "rsi"])
def test_future_mutation_cannot_change_prior_values(name: str) -> None:
    bars = make_bars([10 + index * 0.5 for index in range(40)])
    engine = FeatureEngine()
    context = context_for(bars)
    request = FeatureRequest(
        name=name,
        version=1,
        parameters={"period": 5} if name == "rsi" else {"window": 5},
    )
    before = values(engine.compute_feature(bars, request, context))
    mutated = list(bars)
    mutated[30] = mutated[30].model_copy(update={"close": Decimal("999"), "high": Decimal("1000")})
    after = values(engine.compute_feature(mutated, request, context))
    assert before[:30] == after[:30]


def test_appending_future_observations_preserves_history() -> None:
    bars = make_bars([10 + index for index in range(30)])
    future = make_bars([40 + index for index in range(5)])
    future = [
        bar.model_copy(update={"timestamp": BASE + (30 + index) * Timeframe.H1.duration})
        for index, bar in enumerate(future)
    ]
    engine = FeatureEngine()
    context = context_for(bars)
    request = FeatureRequest(name="volatility_percentile", version=1, parameters={"window": 3})
    original = values(engine.compute_feature(bars, request, context))
    appended = values(engine.compute_feature(bars + future, request, context))
    assert original == appended[: len(original)]


def test_availability_is_after_bar_close_and_decision_lag_is_explicit() -> None:
    bars = make_bars([1, 2, 3])
    context = FeatureContext(
        instrument=bars[0].instrument,
        timeframe=bars[0].timeframe,
        source_dataset_version="fixture-v1",
        decision_lag=timedelta(minutes=5),
        computation_timestamp=BASE,
    )
    result = FeatureEngine().compute_feature(
        bars, FeatureRequest(name="simple_return", version=1), context
    )
    observation = result.observations[1]
    assert observation.availability_timestamp == bars[1].timestamp + timedelta(hours=1)
    assert observation.decision_timestamp == (
        observation.availability_timestamp + timedelta(minutes=5)
    )


def test_timezone_adjustment_and_ordering_contracts_are_enforced() -> None:
    bars = make_bars([1, 2, 3])
    engine = FeatureEngine()
    naive = bars[1].model_copy(update={"timestamp": datetime(2024, 1, 2, 1)})
    with pytest.raises(FeatureValidationError):
        engine.compute_feature(
            [bars[0], naive, bars[2]],
            FeatureRequest(name="sma", version=1),
            context_for(bars),
        )
    adjusted_context = context_for(bars, adjustment_policy=AdjustmentPolicy.ADJUSTED)
    with pytest.raises(FeatureValidationError):
        engine.compute_feature(bars, FeatureRequest(name="sma", version=1), adjusted_context)


def test_equity_volume_is_available_but_fx_volume_feature_is_rejected() -> None:
    bars = make_bars([10, 11], asset_class=AssetClass.EQUITY, volume=[100, None])
    result = FeatureEngine().compute_feature(
        bars, FeatureRequest(name="dollar_volume", version=1), context_for(bars)
    )
    assert values(result) == [1000.0, None]
    assert result.observations[1].status is FeatureStatus.MISSING_INPUT

    fx = make_bars([1, 2])
    with pytest.raises(FeatureConfigurationError):
        FeatureEngine().compute_feature(
            fx, FeatureRequest(name="equity_volume", version=1), context_for(fx)
        )


def test_feature_set_and_storage_are_idempotent_and_bounded() -> None:
    bars = make_bars([1, 2, 3, 4])
    context = context_for(bars)
    feature_set = FeatureEngine().compute_feature_set(
        bars,
        [
            FeatureRequest(name="simple_return", version=1),
            FeatureRequest(name="sma", version=1, parameters={"window": 2}),
        ],
        context,
    )
    store = InMemoryFeatureStore()
    assert store.upsert(feature_set) == len(feature_set.observations)
    assert store.upsert(feature_set) == 0
    queried = store.query(
        symbol="EUR/USD",
        timeframe=Timeframe.H1,
        start=bars[1].timestamp,
        end=bars[2].timestamp,
        dataset_version="fixture-v1",
        adjustment_policy=AdjustmentPolicy.RAW,
    )
    assert len(queried) == 4
    assert all(
        bars[1].timestamp <= item.observation_timestamp <= bars[2].timestamp for item in queried
    )


def test_duplicate_and_unsorted_inputs_fail_loudly() -> None:
    bars = make_bars([1, 2, 3])
    engine = FeatureEngine()
    with pytest.raises(FeatureValidationError):
        engine.compute_feature(
            [bars[0], bars[1], bars[1]],
            FeatureRequest(name="sma", version=1),
            context_for(bars),
        )
    with pytest.raises(FeatureValidationError):
        engine.compute_feature(
            [bars[1], bars[0], bars[2]],
            FeatureRequest(name="sma", version=1),
            context_for(bars),
        )


def test_date_bounds_wrappers_and_storage_latest_are_bounded() -> None:
    bars = make_bars([1, 2, 3, 4])
    context = context_for(bars, start=bars[1].timestamp, end=bars[2].timestamp)
    engine = FeatureEngine()
    result = engine.compute_feature(bars, FeatureRequest(name="simple_return", version=1), context)
    assert [item.observation_timestamp for item in result.observations] == [
        bars[1].timestamp,
        bars[2].timestamp,
    ]
    assert result.feature_keys == frozenset({("simple_return", 1)})
    assert (
        compute_feature(bars, FeatureRequest(name="simple_return", version=1), context).observations
        == result.observations
    )
    assert len(
        compute_feature_set(
            bars,
            [FeatureRequest(name="simple_return", version=1)],
            context,
        ).observations
    ) == len(result.observations)

    store = InMemoryFeatureStore()
    store.upsert(result)
    assert len(store) == len(result.observations)
    assert (
        store.latest(
            symbol="EUR/USD",
            timeframe=Timeframe.H1,
            dataset_version="fixture-v1",
            adjustment_policy=AdjustmentPolicy.RAW,
        )
        == result.observations[-1]
    )
    with pytest.raises(FeatureValidationError):
        store.query(
            symbol="EUR/USD",
            timeframe=Timeframe.H1,
            start=bars[2].timestamp,
            end=bars[1].timestamp,
            dataset_version="fixture-v1",
            adjustment_policy=AdjustmentPolicy.RAW,
        )


def test_validation_boundary_reports_mixed_context_and_bad_feature_sets() -> None:
    bars = make_bars([1, 2, 3])
    context = context_for(bars)
    validate_bar_series(bars, context)
    with pytest.raises(FeatureValidationError):
        validate_bar_series([], context)
    with pytest.raises(FeatureValidationError):
        validate_bar_series(
            bars,
            context.model_copy(
                update={
                    "instrument": Instrument(
                        canonical_symbol="GBP/USD", asset_class=AssetClass.FOREX
                    )
                }
            ),
        )
    with pytest.raises(FeatureValidationError):
        validate_bar_series(
            bars,
            context.model_copy(update={"timeframe": Timeframe.D1}),
        )
    with pytest.raises(FeatureValidationError):
        validate_bar_series(
            [bars[0], bars[1].model_copy(update={"close": Decimal("-1")}), bars[2]],
            context,
        )

    feature_set = FeatureEngine().compute_feature(
        bars, FeatureRequest(name="simple_return", version=1), context
    )
    with pytest.raises(FeatureValidationError):
        validate_feature_set(
            feature_set.model_copy(
                update={"observations": feature_set.observations + (feature_set.observations[0],)}
            )
        )
    with pytest.raises(FeatureValidationError):
        validate_feature_set(
            feature_set.model_copy(
                update={"observations": tuple(reversed(feature_set.observations))}
            )
        )
    altered = feature_set.observations[1].model_copy(
        update={"instrument": Instrument(canonical_symbol="GBP/USD", asset_class=AssetClass.FOREX)}
    )
    with pytest.raises(FeatureValidationError):
        validate_feature_set(
            feature_set.model_copy(update={"observations": (feature_set.observations[0], altered)})
        )


def test_feature_observation_contract_rejects_nonfinite_and_bad_causal_order() -> None:
    bars = make_bars([1, 2, 3])
    observation = (
        FeatureEngine()
        .compute_feature(bars, FeatureRequest(name="simple_return", version=1), context_for(bars))
        .observations[1]
    )
    payload = observation.model_dump()
    payload["value"] = float("inf")
    with pytest.raises(ValidationError):
        FeatureObservation(**payload)
    payload = observation.model_dump()
    payload["decision_timestamp"] = payload["availability_timestamp"] - timedelta(seconds=1)
    with pytest.raises(ValidationError):
        FeatureObservation(**payload)
    payload = observation.model_dump()
    payload["status"] = FeatureStatus.WARMUP
    with pytest.raises(ValidationError):
        FeatureObservation(**payload)
    payload = observation.model_dump()
    payload["availability_timestamp"] = payload["observation_timestamp"] - timedelta(seconds=1)
    with pytest.raises(ValidationError):
        FeatureObservation(**payload)
    payload = observation.model_dump()
    payload["status"] = FeatureStatus.VALUE
    payload["value"] = None
    with pytest.raises(ValidationError):
        FeatureObservation(**payload)
    payload = observation.model_dump()
    payload["status"] = FeatureStatus.WARMUP
    payload["value"] = 1.0
    with pytest.raises(ValidationError):
        FeatureObservation(**payload)
    payload = observation.model_dump()
    payload["lineage"]["feature_name"] = "other"
    with pytest.raises(ValidationError):
        FeatureObservation(**payload)
    payload = observation.model_dump()
    payload["lineage"]["feature_version"] = 2
    with pytest.raises(ValidationError):
        FeatureObservation(**payload)
    payload = observation.model_dump()
    payload["lineage"]["symbol"] = "GBP/USD"
    with pytest.raises(ValidationError):
        FeatureObservation(**payload)


def test_custom_registered_definition_without_implementation_fails_closed() -> None:
    definition = FeatureDefinition(
        name="controlled_missing",
        version=1,
        description="A registry-only definition used to test fail-closed behavior.",
        applicable_asset_classes=frozenset({AssetClass.FOREX}),
        applicable_timeframes=frozenset({Timeframe.H1}),
        required_columns=("close",),
        lookback=1,
        computation="controlled_missing",
    )
    registry = build_default_registry()
    registry.register(definition)
    bars = make_bars([1, 2])
    with pytest.raises(FeatureConfigurationError):
        FeatureEngine(registry).compute_feature(
            bars, FeatureRequest(name="controlled_missing", version=1), context_for(bars)
        )
    with pytest.raises(FeatureConfigurationError):
        registry.register(
            FeatureDefinition(
                name="missing_dependency",
                version=1,
                description="invalid dependency",
                applicable_asset_classes=frozenset({AssetClass.FOREX}),
                applicable_timeframes=frozenset({Timeframe.H1}),
                required_columns=("close",),
                dependencies=("not_registered",),
                lookback=1,
                computation="missing_dependency",
            )
        )
