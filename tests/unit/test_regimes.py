"""Adversarial causality, boundary, and contract tests for Phase 5 regimes."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from traderos.data.bars import MarketBar
from traderos.data.calendars import ForexCalendar, UsEquityCalendar
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.quality import DataQualityEvent, QualityCode, QualitySeverity
from traderos.data.timeframes import Timeframe
from traderos.features import FeatureContext, FeatureEngine, FeatureRequest, FeatureStatus
from traderos.features.errors import FeatureConfigurationError
from traderos.features.registry import build_default_registry
from traderos.regimes import (
    BaselineRegimeParameters,
    DataSessionState,
    EmaPercentileRegimeDetector,
    FeatureProvenance,
    FeatureRequirement,
    LiquidityRegime,
    RegimeAnalysisError,
    RegimeCausalityError,
    RegimeConfigurationError,
    RegimeContext,
    RegimeEngine,
    RegimeMetadata,
    RegimeRegistry,
    RegimeRegistryError,
    TrendRegime,
    VolatilityRegime,
    analyze_regime_stability,
)

BASE = datetime(2024, 1, 2, tzinfo=UTC)


def instrument(asset_class: AssetClass = AssetClass.FOREX, symbol: str | None = None) -> Instrument:
    if asset_class is AssetClass.FOREX:
        return Instrument(
            canonical_symbol=symbol or "EUR/USD",
            asset_class=asset_class,
            base_currency="EUR",
            quote_currency="USD",
            trading_currency="USD",
        )
    return Instrument(
        canonical_symbol=symbol or "AAPL",
        asset_class=asset_class,
        exchange="NASDAQ",
        trading_currency="USD",
        timezone="America/New_York",
    )


def make_bars(
    closes: list[float],
    *,
    asset_class: AssetClass = AssetClass.FOREX,
    symbol: str | None = None,
    timeframe: Timeframe = Timeframe.H1,
    spread: float | None = 0.01,
    volume: float | None = 1000.0,
    bid: float | None = None,
    ask: float | None = None,
    start: datetime = BASE,
) -> list[MarketBar]:
    item = instrument(asset_class, symbol)
    return [
        MarketBar(
            instrument=item,
            timeframe=timeframe,
            timestamp=start + timeframe.duration * index,
            open=Decimal(str(close - 0.25)),
            high=Decimal(str(close + 0.5)),
            low=Decimal(str(close - 0.5)),
            close=Decimal(str(close)),
            volume=None if volume is None else Decimal(str(volume)),
            spread=None if spread is None else Decimal(str(spread)),
            bid=None if bid is None else Decimal(str(bid)),
            ask=None if ask is None else Decimal(str(ask)),
            source="fixture",
            currency="USD",
            ingestion_timestamp=BASE,
        )
        for index, close in enumerate(closes)
    ]


def detector(parameters: BaselineRegimeParameters | None = None) -> EmaPercentileRegimeDetector:
    return EmaPercentileRegimeDetector(
        parameters
        or BaselineRegimeParameters(
            fast_ema_period=3,
            slow_ema_period=5,
            volatility_percentile_window=4,
            minimum_trend_separation=0.01,
            maximum_spread_fraction=0.001,
        )
    )


def feature_observations(item: EmaPercentileRegimeDetector, bars: list[MarketBar]):
    requests = [
        FeatureRequest(
            name=requirement.name,
            version=requirement.version,
            parameters=dict(requirement.parameters),
        )
        for requirement in item.metadata.required_features
    ]
    return (
        FeatureEngine()
        .compute_feature_set(
            bars,
            requests,
            FeatureContext(
                instrument=bars[0].instrument,
                timeframe=bars[0].timeframe,
                source_dataset_version="fixture-v1",
                computation_timestamp=BASE,
            ),
        )
        .observations
    )


def context(
    item: EmaPercentileRegimeDetector,
    bars: list[MarketBar],
    index: int,
    *,
    observations=None,
    calendar=None,
    quality_events: tuple[DataQualityEvent, ...] = (),
):
    all_observations = feature_observations(item, bars) if observations is None else observations
    current = bars[index]
    decision = current.timestamp + current.timeframe.duration
    return RegimeContext(
        bar=current,
        decision_timestamp=decision,
        feature_observations=tuple(
            observation
            for observation in all_observations
            if observation.observation_timestamp <= current.timestamp
            and observation.availability_timestamp <= decision
        ),
        calendar=calendar or ForexCalendar(),
        detector_parameters=item.parameters,
        quality_events=quality_events,
    )


def set_current_values(item, observations, bar, fast: float, slow: float, percentile: float):
    values = (fast, slow, percentile)
    result = []
    for observation in observations:
        for requirement, value in zip(item.metadata.required_features, values, strict=True):
            if observation.observation_timestamp == bar.timestamp and requirement.matches(
                observation
            ):
                result.append(
                    observation.model_copy(update={"value": value, "status": FeatureStatus.VALUE})
                )
                break
        else:
            result.append(observation)
    return tuple(result)


def active_state(item=None, bars=None, index: int = 25):
    item = item or detector()
    bars = bars or make_bars([100 + (number * number * 0.02) for number in range(35)])
    return RegimeEngine(item).evaluate(context(item, bars, index))


def test_baseline_metadata_declares_version_scope_features_and_parameters() -> None:
    item = detector()
    metadata = item.metadata
    assert metadata.regime_detector_id == "ema_percentile_regime"
    assert metadata.regime_detector_version == "1"
    assert metadata.required_features[0].parameters == (("window", 3),)
    assert metadata.required_features[-1].name == "volatility_percentile"
    assert set(metadata.parameter_schema) == set(item.parameters)
    assert AssetClass.FOREX in metadata.supported_asset_classes
    assert Timeframe.H1 in metadata.supported_timeframes


@pytest.mark.parametrize(
    "factory",
    [
        lambda: BaselineRegimeParameters(fast_ema_period=5, slow_ema_period=5),
        lambda: BaselineRegimeParameters(
            low_volatility_percentile=0.8, high_volatility_percentile=0.2
        ),
        lambda: BaselineRegimeParameters(maximum_spread_fraction=float("nan")),
        lambda: BaselineRegimeParameters(fast_ema_period=3.0),
    ],
)
def test_parameters_are_strict_and_safe(factory) -> None:
    with pytest.raises(ValueError):
        factory()


def test_trend_and_volatility_boundaries_are_unambiguous() -> None:
    item = detector(
        BaselineRegimeParameters(
            fast_ema_period=3,
            slow_ema_period=5,
            minimum_trend_separation=0.1,
            volatility_percentile_window=4,
            low_volatility_percentile=0.25,
            high_volatility_percentile=0.75,
        )
    )
    bars = make_bars([100 + number for number in range(30)])
    observations = feature_observations(item, bars)
    current = bars[-1]
    engine = RegimeEngine(item)

    equal = engine.evaluate(
        context(
            item,
            bars,
            -1,
            observations=set_current_values(item, observations, current, 100, 100, 0.25),
        )
    )
    assert equal.trend is TrendRegime.NEUTRAL
    assert equal.volatility is VolatilityRegime.NORMAL
    at_threshold = engine.evaluate(
        context(
            item,
            bars,
            -1,
            observations=set_current_values(item, observations, current, 110, 100, 0.75),
        )
    )
    assert at_threshold.trend is TrendRegime.NEUTRAL
    assert at_threshold.volatility is VolatilityRegime.NORMAL
    above = engine.evaluate(
        context(
            item,
            bars,
            -1,
            observations=set_current_values(item, observations, current, 110.001, 100, 0.751),
        )
    )
    assert above.trend is TrendRegime.TRENDING_UP
    assert above.volatility is VolatilityRegime.HIGH
    below = engine.evaluate(
        context(
            item,
            bars,
            -1,
            observations=set_current_values(item, observations, current, 89.999, 100, 0.249),
        )
    )
    assert below.trend is TrendRegime.TRENDING_DOWN
    assert below.volatility is VolatilityRegime.LOW


def test_warmup_and_missing_or_undefined_feature_inputs_are_explicit() -> None:
    item = detector()
    bars = make_bars([100 + number for number in range(10)])
    warmup = RegimeEngine(item).evaluate(context(item, bars, 3))
    assert warmup.data_session is DataSessionState.WARMUP
    assert warmup.trend is TrendRegime.UNAVAILABLE
    assert warmup.volatility is VolatilityRegime.UNAVAILABLE

    early_full = feature_observations(item, bars)
    early_missing = tuple(
        observation
        for observation in early_full
        if not (
            observation.observation_timestamp == bars[3].timestamp
            and observation.feature_name == "volatility_percentile"
        )
    )
    assert (
        RegimeEngine(item).evaluate(context(item, bars, 3, observations=early_missing)).data_session
        is DataSessionState.DATA_UNAVAILABLE
    )

    full = feature_observations(item, bars)
    missing = tuple(
        observation
        for observation in full
        if not (
            observation.observation_timestamp == bars[-1].timestamp
            and observation.feature_name == "volatility_percentile"
        )
    )
    unavailable = RegimeEngine(item).evaluate(context(item, bars, -1, observations=missing))
    assert unavailable.data_session is DataSessionState.DATA_UNAVAILABLE

    invalid = set_current_values(item, full, bars[-1], 100, 99, float("inf"))
    invalid_state = RegimeEngine(item).evaluate(context(item, bars, -1, observations=invalid))
    assert invalid_state.data_session is DataSessionState.DATA_UNAVAILABLE


def test_liquidity_uses_observed_evidence_and_never_fabricates_normality() -> None:
    item = detector()
    normal = active_state(item, make_bars([100 + number * number * 0.02 for number in range(35)]))
    assert normal.liquidity is LiquidityRegime.NORMAL

    stressed_bars = make_bars([100 + number * number * 0.02 for number in range(35)], spread=1.0)
    assert active_state(item, stressed_bars).liquidity is LiquidityRegime.STRESSED

    missing_spread = make_bars([100 + number * number * 0.02 for number in range(35)], spread=None)
    assert active_state(item, missing_spread).liquidity is LiquidityRegime.UNKNOWN

    quoted = make_bars(
        [100 + number * number * 0.02 for number in range(35)],
        spread=None,
        bid=99.99,
        ask=100.01,
    )
    quote_state = active_state(item, quoted)
    assert quote_state.liquidity is LiquidityRegime.NORMAL
    assert quote_state.market_data_provenance == ("market_bar:bid_ask",)

    equity_start = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    equity = make_bars(
        [100 + number * number * 0.02 for number in range(35)],
        asset_class=AssetClass.EQUITY,
        start=equity_start,
        volume=None,
    )
    state = RegimeEngine(item).evaluate(context(item, equity, 25, calendar=UsEquityCalendar()))
    assert state.liquidity is LiquidityRegime.UNKNOWN
    liquid_equity = make_bars(
        [100 + number * number * 0.02 for number in range(35)],
        asset_class=AssetClass.EQUITY,
        start=equity_start,
    )
    assert RegimeEngine(item).evaluate(
        context(item, liquid_equity, 25, calendar=UsEquityCalendar())
    ).market_data_provenance == ("market_bar:spread", "market_bar:volume")


def test_calendar_closure_and_quality_errors_do_not_become_market_regimes() -> None:
    item = detector()
    bars = make_bars([100 + number * number * 0.02 for number in range(35)])
    closed_bar = bars[-1].model_copy(update={"timestamp": datetime(2024, 1, 6, tzinfo=UTC)})
    closed_context = RegimeContext(
        bar=closed_bar,
        decision_timestamp=closed_bar.timestamp + closed_bar.timeframe.duration,
        feature_observations=(),
        calendar=ForexCalendar(),
        detector_parameters=item.parameters,
    )
    closed = RegimeEngine(item).evaluate(closed_context)
    assert closed.data_session is DataSessionState.INACTIVE
    assert closed.trend is TrendRegime.UNAVAILABLE

    current = bars[25]
    stale = DataQualityEvent(
        occurred_at=current.timestamp + current.timeframe.duration,
        severity=QualitySeverity.ERROR,
        code=QualityCode.STALE_DATA,
        message="stale fixture data",
        symbol=current.symbol,
        timeframe=current.timeframe.value,
        bar_timestamp=current.timestamp,
    )
    unavailable = RegimeEngine(item).evaluate(context(item, bars, 25, quality_events=(stale,)))
    assert unavailable.data_session is DataSessionState.DATA_UNAVAILABLE
    assert unavailable.liquidity is LiquidityRegime.UNKNOWN


def test_future_feature_is_rejected_at_context_boundary() -> None:
    item = detector()
    bars = make_bars([100 + number for number in range(35)])
    future = feature_observations(item, bars)[-1]
    with pytest.raises(RegimeCausalityError, match="future feature"):
        RegimeContext(
            bar=bars[20],
            decision_timestamp=bars[20].timestamp + bars[20].timeframe.duration,
            feature_observations=(future,),
            calendar=ForexCalendar(),
            detector_parameters=item.parameters,
        )


def test_future_mutation_append_and_normalization_cannot_change_history() -> None:
    item = detector()
    original_bars = make_bars([100 + number * number * 0.02 for number in range(45)])
    index = 30
    original = active_state(item, original_bars, index)
    mutated = list(original_bars)
    mutated[-1] = mutated[-1].model_copy(
        update={
            "open": Decimal("1"),
            "high": Decimal("500"),
            "low": Decimal("0.5"),
            "close": Decimal("400"),
        }
    )
    assert active_state(item, mutated, index) == original

    appended = original_bars + make_bars(
        [600 + number * 100 for number in range(10)],
        start=original_bars[-1].timestamp + original_bars[-1].timeframe.duration,
    )
    appended_state = active_state(item, appended, index)
    assert appended_state == original

    engine = RegimeEngine(item)
    original_states = tuple(
        engine.evaluate(context(item, original_bars, number)) for number in range(20, 40)
    )
    appended_states = tuple(
        engine.evaluate(context(item, appended, number)) for number in range(20, 40)
    )
    assert appended_states == original_states


def test_instrument_isolation_determinism_and_parameter_identity() -> None:
    item = detector()
    target = make_bars([100 + number * number * 0.02 for number in range(35)])
    target_state = active_state(item, target)
    unrelated = make_bars(
        [1000 - number * 25 for number in range(35)], symbol="GBP/USD", spread=10.0
    )
    assert active_state(item, target) == target_state
    assert unrelated[0].symbol == "GBP/USD"
    assert active_state(item, target) == active_state(item, target)

    changed = detector(
        BaselineRegimeParameters(
            fast_ema_period=3,
            slow_ema_period=5,
            volatility_percentile_window=4,
            minimum_trend_separation=0.02,
        )
    )
    assert item.configuration_id != changed.configuration_id
    assert active_state(changed, target).configuration_id == changed.configuration_id


def test_unsupported_timeframes_and_context_parameter_mismatches_fail() -> None:
    item = EmaPercentileRegimeDetector(
        BaselineRegimeParameters(
            fast_ema_period=3, slow_ema_period=5, volatility_percentile_window=4
        ),
        supported_timeframes=frozenset({Timeframe.H1}),
    )
    bars = make_bars([100 + number * number * 0.02 for number in range(35)], timeframe=Timeframe.H4)
    with pytest.raises(RegimeConfigurationError, match="does not support"):
        RegimeEngine(item).evaluate(context(item, bars, 25))

    h1_bars = make_bars([100 + number * number * 0.02 for number in range(35)])
    unsafe = replace(context(item, h1_bars, 25), detector_parameters={"wrong": 1})
    with pytest.raises(RegimeConfigurationError, match="parameters"):
        RegimeEngine(item).evaluate(unsafe)


def test_context_rejects_cross_series_future_quality_and_nonhistorical_prior_state() -> None:
    item = detector()
    bars = make_bars([100 + number * number * 0.02 for number in range(35)])
    other = make_bars([100 + number for number in range(35)], symbol="GBP/USD")
    observation = feature_observations(item, bars)[-1].model_copy(
        update={"instrument": other[0].instrument}
    )
    with pytest.raises(RegimeCausalityError, match="crosses instruments"):
        RegimeContext(
            bar=bars[-1],
            decision_timestamp=bars[-1].timestamp + bars[-1].timeframe.duration,
            feature_observations=(observation,),
            calendar=ForexCalendar(),
            detector_parameters=item.parameters,
        )
    future_event = DataQualityEvent(
        occurred_at=bars[-1].timestamp + bars[-1].timeframe.duration * 2,
        severity=QualitySeverity.ERROR,
        code=QualityCode.STALE_DATA,
        message="future",
    )
    with pytest.raises(RegimeCausalityError, match="future quality"):
        context(item, bars, -1, quality_events=(future_event,))
    future_bar_event = DataQualityEvent(
        occurred_at=bars[25].timestamp + bars[25].timeframe.duration,
        severity=QualitySeverity.ERROR,
        code=QualityCode.MISSING_BAR,
        message="future bar",
        bar_timestamp=bars[26].timestamp,
    )
    with pytest.raises(RegimeCausalityError, match="future quality-bar"):
        context(item, bars, 25, quality_events=(future_bar_event,))
    state = active_state(item, bars, 25)
    with pytest.raises(RegimeCausalityError, match="not historical"):
        replace(context(item, bars, 25), prior_state=state)


def test_registry_validates_version_identity_and_feature_dependencies() -> None:
    item = detector()
    registry = RegimeRegistry(build_default_registry())
    registry.register(item)
    assert registry.get(item.regime_detector_id, item.regime_detector_version) is item
    assert registry.all() == (item,)
    with pytest.raises(RegimeRegistryError):
        registry.register(item)
    with pytest.raises(RegimeRegistryError):
        registry.get("missing", "1")

    class MissingFeatureDetector:
        regime_detector_id = "missing"
        regime_detector_version = "1"
        parameters = {}

        @property
        def metadata(self):
            return replace(
                item.metadata,
                regime_detector_id="missing",
                required_features=(
                    item.metadata.required_features[0].from_parameters("missing", 1, {}),
                ),
            )

        def evaluate(self, current):
            return current

    with pytest.raises(FeatureConfigurationError):
        registry.register(MissingFeatureDetector())


def test_engine_series_is_ordered_single_series_and_observational() -> None:
    item = detector()
    bars = make_bars([100 + number * number * 0.02 for number in range(35)])
    contexts = tuple(context(item, bars, number) for number in range(20, 26))
    states = RegimeEngine(item).evaluate_series(contexts)
    assert [state.decision_timestamp for state in states] == sorted(
        state.decision_timestamp for state in states
    )
    with pytest.raises(RegimeCausalityError, match="strictly ordered"):
        RegimeEngine(item).evaluate_series(tuple(reversed(contexts)))


def test_stability_metrics_are_descriptive_and_validate_history() -> None:
    item = detector()
    bars = make_bars([100 + number * number * 0.02 for number in range(35)])
    states = RegimeEngine(item).evaluate_series(
        tuple(context(item, bars, number) for number in range(20, 26))
    )
    report = analyze_regime_stability(states)
    assert report.observation_count == 6
    trend = next(result for result in report.dimensions if result.dimension.value == "trend")
    assert sum(share.percentage for share in trend.shares) == pytest.approx(1.0)
    assert trend.transition_frequency <= 1.0
    assert sum(run.observation_count for run in trend.runs) == 6
    with pytest.raises(RegimeAnalysisError):
        analyze_regime_stability(())
    with pytest.raises(RegimeAnalysisError, match="strictly ordered"):
        analyze_regime_stability(tuple(reversed(states)))


def test_contract_models_and_context_guards_fail_closed() -> None:
    item = detector()
    bars = make_bars([100 + number * number * 0.02 for number in range(35)])
    current_observation = next(
        candidate
        for candidate in feature_observations(item, bars)
        if candidate.observation_timestamp == bars[25].timestamp
    )
    with pytest.raises(ValueError):
        FeatureRequirement("", 1)
    with pytest.raises(ValueError):
        FeatureRequirement("ema", 0)
    with pytest.raises(ValueError):
        FeatureRequirement("ema", 1, (("window", 3), ("window", 5)))
    with pytest.raises(ValueError):
        FeatureProvenance(item.metadata.required_features[0], BASE, BASE.replace(year=2023))
    with pytest.raises(ValueError):
        RegimeMetadata(
            "", "1", frozenset({AssetClass.FOREX}), frozenset({Timeframe.H1}), (), (), "x"
        )
    with pytest.raises(ValueError):
        RegimeMetadata("x", "1", frozenset(), frozenset({Timeframe.H1}), (), (), "x")
    with pytest.raises(ValueError):
        RegimeMetadata("x", "1", frozenset({AssetClass.FOREX}), frozenset(), (), (), "x")
    with pytest.raises(ValueError):
        RegimeMetadata(
            "x", "1", frozenset({AssetClass.FOREX}), frozenset({Timeframe.H1}), (), ("x", "x"), "x"
        )
    with pytest.raises(ValueError):
        RegimeMetadata(
            "x", "1", frozenset({AssetClass.FOREX}), frozenset({Timeframe.H1}), (), (), " "
        )
    with pytest.raises(RegimeCausalityError, match="precedes"):
        replace(context(item, bars, 25), decision_timestamp=bars[25].timestamp)
    with pytest.raises(RegimeCausalityError, match="unavailable"):
        replace(
            context(item, bars, 25),
            feature_observations=(
                current_observation.model_copy(
                    update={"availability_timestamp": bars[25].timestamp + Timeframe.D1.duration}
                ),
            ),
        )
    with pytest.raises(RegimeCausalityError, match="crosses timeframes"):
        replace(
            context(item, bars, 25),
            feature_observations=(
                current_observation.model_copy(update={"timeframe": Timeframe.D1}),
            ),
        )


def test_state_guards_scope_and_observational_series_isolation() -> None:
    item = detector()
    bars = make_bars([100 + number * number * 0.02 for number in range(35)])
    state = active_state(item, bars)
    with pytest.raises(ValueError):
        replace(state, asset_class=AssetClass.EQUITY)
    with pytest.raises(ValueError):
        replace(state, state_id="")
    with pytest.raises(ValueError):
        replace(state, trend_strength=float("nan"))

    equity_start = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    equity = make_bars(
        [100 + number * number * 0.02 for number in range(35)],
        asset_class=AssetClass.EQUITY,
        start=equity_start,
    )
    forex_only = EmaPercentileRegimeDetector(
        item.config,
        supported_asset_classes=frozenset({AssetClass.FOREX}),
    )
    with pytest.raises(RegimeConfigurationError, match="does not support"):
        RegimeEngine(forex_only).evaluate(
            context(forex_only, equity, 25, calendar=UsEquityCalendar())
        )
    other = make_bars([100 + number for number in range(35)], symbol="GBP/USD")
    with pytest.raises(RegimeCausalityError, match="one instrument"):
        RegimeEngine(item).evaluate_series((context(item, bars, 25), context(item, other, 26)))


def test_registry_lookup_variants_and_stability_configuration_guard() -> None:
    item = detector()
    registry = RegimeRegistry()
    registry.register(item)
    assert registry.get(item.regime_detector_id) is item
    second = detector()
    second.regime_detector_version = "2"
    registry.register(second)
    with pytest.raises(RegimeRegistryError, match="requires an explicit version"):
        registry.get(item.regime_detector_id)

    class MismatchedDetector:
        regime_detector_id = "mismatched"
        regime_detector_version = "1"
        parameters = {}

        @property
        def metadata(self):
            return replace(item.metadata, regime_detector_id="different")

        def evaluate(self, current):
            return current

    with pytest.raises(RegimeRegistryError, match="attributes"):
        registry.register(MismatchedDetector())

    states = RegimeEngine(item).evaluate_series(
        tuple(
            context(item, make_bars([100 + number * number * 0.02 for number in range(35)]), index)
            for index in range(20, 22)
        )
    )
    with pytest.raises(RegimeAnalysisError, match="configuration"):
        analyze_regime_stability((states[0], replace(states[1], configuration_id="other")))
