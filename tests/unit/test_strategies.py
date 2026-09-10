"""Adversarial unit and Phase 3 integration tests for Phase 4 strategies."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from traderos.backtesting import (
    BacktestConfig,
    BacktestEngine,
    CostConfig,
    OrderSide,
    OrderType,
    SlippageConfig,
    SpreadConfig,
)
from traderos.backtesting.models import StrategyContext as BacktestContext
from traderos.data.bars import MarketBar
from traderos.data.errors import TimestampNormalizationError
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.lineage import AdjustmentPolicy
from traderos.data.timeframes import Timeframe
from traderos.features import FeatureContext, FeatureEngine, FeatureRequest, build_default_registry
from traderos.features.errors import FeatureConfigurationError
from traderos.strategies import (
    BacktestStrategyAdapter,
    BreakoutParameters,
    EquityBreakoutStrategy,
    EquityMomentumStrategy,
    FeatureRequirement,
    ForexBreakoutStrategy,
    ForexTrendFollowingStrategy,
    MissingFeatureError,
    MomentumParameters,
    SignalDirection,
    StrategyCausalityError,
    StrategyConfigurationError,
    StrategyContext,
    StrategyMetadata,
    StrategyRegistry,
    StrategyRegistryError,
    StrategyResult,
    StrategySignal,
    TrendParameters,
    build_default_strategy_registry,
)
from traderos.strategies.base import RuleStrategy
from traderos.strategies.models import OrderIntent

BASE = datetime(2024, 1, 2, tzinfo=UTC)


def instrument(asset_class: AssetClass, symbol: str) -> Instrument:
    if asset_class is AssetClass.FOREX:
        return Instrument(
            canonical_symbol=symbol,
            asset_class=asset_class,
            base_currency="EUR",
            quote_currency="USD",
            trading_currency="USD",
            provider_symbols={"fixture": symbol},
        )
    return Instrument(
        canonical_symbol=symbol,
        asset_class=asset_class,
        exchange="NASDAQ",
        trading_currency="USD",
        provider_symbols={"fixture": symbol},
    )


def make_bars(
    prices: list[str],
    *,
    asset_class: AssetClass,
    symbol: str,
    timeframe: Timeframe = Timeframe.H1,
    highs: list[str] | None = None,
    lows: list[str] | None = None,
) -> list[MarketBar]:
    item = instrument(asset_class, symbol)
    highs = highs or [str(Decimal(price) + 1) for price in prices]
    lows = lows or [str(Decimal(price) - 1) for price in prices]
    return [
        MarketBar(
            instrument=item,
            timeframe=timeframe,
            adjustment_policy=AdjustmentPolicy.RAW,
            timestamp=BASE + timeframe.duration * index,
            open=Decimal(price),
            high=Decimal(highs[index]),
            low=Decimal(lows[index]),
            close=Decimal(price),
            volume=Decimal("1000"),
            source="fixture",
            currency="USD",
            ingestion_timestamp=BASE,
        )
        for index, price in enumerate(prices)
    ]


def feature_observations(strategy, bars: list[MarketBar]):
    requests = [
        FeatureRequest(name=item.name, version=item.version, parameters=dict(item.parameters))
        for item in strategy.metadata.required_features
    ]
    context = FeatureContext(
        instrument=bars[0].instrument,
        timeframe=bars[0].timeframe,
        source_dataset_version="fixture-v1",
        computation_timestamp=BASE,
    )
    return FeatureEngine().compute_feature_set(bars, requests, context).observations


def backtest_config(strategy, bars: list[MarketBar], **overrides) -> BacktestConfig:
    values = {
        "dataset_version": "fixture-v1",
        "instrument_symbols": (bars[0].symbol,),
        "timeframe": bars[0].timeframe,
        "start": bars[0].timestamp,
        "end": bars[-1].timestamp + bars[-1].timeframe.duration,
        "starting_cash": Decimal("1000"),
        "account_currency": "USD",
        "strategy_id": strategy.strategy_id,
        "strategy_version": strategy.strategy_version,
        "feature_versions": tuple(
            sorted({(item.name, item.version) for item in strategy.metadata.required_features})
        ),
    }
    values.update(overrides)
    return BacktestConfig(**values)


def run_strategy(strategy, bars: list[MarketBar], **config_overrides):
    adapter = BacktestStrategyAdapter(strategy)
    result = BacktestEngine(backtest_config(strategy, bars, **config_overrides)).run(
        bars, adapter, features=feature_observations(strategy, bars)
    )
    return result, adapter


def direct_context(strategy, bars: list[MarketBar], *, index: int, observations=None):
    if observations is None:
        observations = feature_observations(strategy, bars)
    current = bars[index]
    visible = tuple(
        item
        for item in observations
        if item.observation_timestamp <= current.timestamp
        and item.availability_timestamp <= current.timestamp + current.timeframe.duration
    )
    return StrategyContext(
        event_timestamp=current.timestamp + current.timeframe.duration,
        bar=current,
        feature_observations=visible,
        position_quantity=Decimal("0"),
        cash=Decimal("1000"),
        equity=Decimal("1000"),
        parameters=strategy.parameters,
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.strategy_version,
    )


@pytest.mark.parametrize(
    ("strategy", "bars"),
    [
        (
            ForexTrendFollowingStrategy(TrendParameters(fast_period=2, slow_period=4)),
            make_bars(
                ["100", "101", "102", "103", "104", "105"],
                asset_class=AssetClass.FOREX,
                symbol="EUR/USD",
            ),
        ),
        (
            ForexBreakoutStrategy(BreakoutParameters(lookback=2)),
            make_bars(["100", "101", "105", "106"], asset_class=AssetClass.FOREX, symbol="EUR/USD"),
        ),
        (
            EquityMomentumStrategy(MomentumParameters(lookback=2, minimum_momentum=0.01)),
            make_bars(["100", "100", "105", "106"], asset_class=AssetClass.EQUITY, symbol="AAPL"),
        ),
        (
            EquityBreakoutStrategy(BreakoutParameters(lookback=2)),
            make_bars(["100", "101", "105", "106"], asset_class=AssetClass.EQUITY, symbol="AAPL"),
        ),
    ],
)
def test_each_baseline_runs_through_real_phase3_engine(strategy, bars) -> None:
    result, adapter = run_strategy(
        strategy,
        bars,
        spread=SpreadConfig(fallback_absolute=Decimal("0.02")),
        slippage=SlippageConfig(absolute=Decimal("0.01")),
        commission=CostConfig(per_unit=Decimal("0.01")),
    )
    assert result.event_count == len(bars)
    assert result.equity_curve
    assert result.ledger == tuple(result.ledger)
    assert all(order.strategy_id == strategy.strategy_id for order in result.orders)
    assert len(adapter.decisions) == len(bars)
    if result.fills:
        assert result.fills[0].timestamp >= bars[1].timestamp
        assert result.metrics.total_fees > 0


def test_baseline_rules_have_explicit_direction_and_warmup() -> None:
    trend = ForexTrendFollowingStrategy(TrendParameters(fast_period=2, slow_period=4))
    bars = make_bars(
        ["100", "101", "102", "103", "104"], asset_class=AssetClass.FOREX, symbol="EUR/USD"
    )
    first = trend.on_bar(direct_context(trend, bars, index=0))
    later = trend.on_bar(direct_context(trend, bars, index=4))
    assert first.signal.direction is SignalDirection.HOLD
    assert first.order_intents == ()
    assert later.signal.direction is SignalDirection.LONG
    assert later.signal.reason == "fast_ema_above_slow_ema"

    momentum = EquityMomentumStrategy(MomentumParameters(lookback=2, minimum_momentum=0.0))
    equity_bars = make_bars(["100", "100", "105"], asset_class=AssetClass.EQUITY, symbol="AAPL")
    assert (
        momentum.on_bar(direct_context(momentum, equity_bars, index=0)).signal.direction
        is SignalDirection.HOLD
    )
    assert (
        momentum.on_bar(direct_context(momentum, equity_bars, index=2)).signal.direction
        is SignalDirection.LONG
    )


def test_breakout_uses_previous_window_and_not_current_high() -> None:
    strategy = EquityBreakoutStrategy(BreakoutParameters(lookback=2))
    bars = make_bars(
        ["100", "101", "102"],
        asset_class=AssetClass.EQUITY,
        symbol="AAPL",
        highs=["100", "101", "1000"],
        lows=["99", "100", "101"],
    )
    result = strategy.on_bar(direct_context(strategy, bars, index=2))
    assert result.signal.direction is SignalDirection.LONG
    assert result.signal.reason == "close_above_previous_high"
    assert result.order_intents[0].quantity == Decimal("1")


def test_future_append_and_mutation_do_not_change_historical_decisions() -> None:
    strategy = EquityMomentumStrategy(MomentumParameters(lookback=2, minimum_momentum=0.0))
    base_bars = make_bars(
        ["100", "100", "105", "106"], asset_class=AssetClass.EQUITY, symbol="AAPL"
    )
    future = make_bars(
        ["100", "100", "105", "106", "200", "500"],
        asset_class=AssetClass.EQUITY,
        symbol="AAPL",
    )
    base_result, base_adapter = run_strategy(strategy, base_bars)
    appended_result, appended_adapter = run_strategy(strategy, future)
    mutated = list(future)
    mutated[-1] = mutated[-1].model_copy(
        update={
            "open": Decimal("120"),
            "close": Decimal("120"),
            "high": Decimal("121"),
            "low": Decimal("119"),
        }
    )
    _, mutated_adapter = run_strategy(strategy, mutated)
    base_signals = tuple(item.signal.direction for item in base_adapter.decisions)
    assert tuple(item.signal.direction for item in appended_adapter.decisions[:4]) == base_signals
    assert tuple(item.signal.direction for item in mutated_adapter.decisions[:4]) == base_signals
    assert appended_result.equity_curve[:4] == base_result.equity_curve


def test_instrument_and_timeframe_boundaries_are_enforced() -> None:
    strategy = EquityMomentumStrategy(MomentumParameters(lookback=2))
    equity_bars = make_bars(["100", "101", "102"], asset_class=AssetClass.EQUITY, symbol="AAPL")
    forex_bars = make_bars(["100", "101", "102"], asset_class=AssetClass.FOREX, symbol="EUR/USD")
    with pytest.raises(StrategyConfigurationError):
        strategy.on_bar(direct_context(strategy, forex_bars, index=2))
    with pytest.raises(StrategyCausalityError):
        StrategyContext(
            event_timestamp=equity_bars[2].timestamp + equity_bars[2].timeframe.duration,
            bar=equity_bars[2],
            feature_observations=(
                feature_observations(strategy, equity_bars)[-1].model_copy(
                    update={"instrument": forex_bars[0].instrument}
                ),
            ),
            position_quantity=Decimal("0"),
            cash=Decimal("1000"),
            equity=Decimal("1000"),
            parameters=strategy.parameters,
            strategy_id=strategy.strategy_id,
            strategy_version=strategy.strategy_version,
        )


def test_missing_feature_and_future_feature_fail_explicitly() -> None:
    strategy = EquityMomentumStrategy(MomentumParameters(lookback=2))
    bars = make_bars(["100", "101", "102"], asset_class=AssetClass.EQUITY, symbol="AAPL")
    context = direct_context(strategy, bars, index=2, observations=())
    with pytest.raises(MissingFeatureError):
        strategy.on_bar(context)
    future_observation = feature_observations(strategy, bars)[-1]
    with pytest.raises(StrategyCausalityError):
        StrategyContext(
            event_timestamp=bars[1].timestamp + bars[1].timeframe.duration,
            bar=bars[1],
            feature_observations=(future_observation,),
            position_quantity=Decimal("0"),
            cash=Decimal("1000"),
            equity=Decimal("1000"),
            parameters=strategy.parameters,
            strategy_id=strategy.strategy_id,
            strategy_version=strategy.strategy_version,
        )


def test_registry_validates_identity_scope_and_feature_dependencies() -> None:
    registry = StrategyRegistry(build_default_registry())
    trend = ForexTrendFollowingStrategy(TrendParameters(fast_period=2, slow_period=4))
    registry.register(trend)
    assert registry.get(trend.strategy_id, trend.strategy_version) is trend
    assert registry.all() == (trend,)
    with pytest.raises(StrategyRegistryError):
        registry.register(trend)
    with pytest.raises(StrategyRegistryError):
        registry.get("missing", "1")

    class BadMetadata:
        strategy_id = "bad"
        strategy_version = "1"
        parameters = {}

        @property
        def metadata(self):
            return trend.metadata

        def on_bar(self, context):
            return trend.on_bar(context)

    with pytest.raises(StrategyRegistryError):
        registry.register(BadMetadata())

    class MismatchedMetadata:
        strategy_id = "mismatched"
        strategy_version = "1"
        parameters = {}
        metadata = StrategyMetadata(
            strategy_id="declared",
            strategy_version="1",
            supported_asset_classes=frozenset({AssetClass.FOREX}),
            supported_timeframes=frozenset({Timeframe.H1}),
            required_features=(),
            warmup_bars=0,
            parameter_schema=(),
            description="Mismatched identity test strategy.",
        )

        def on_bar(self, context):
            return context

    with pytest.raises(StrategyRegistryError):
        registry.register(MismatchedMetadata())

    no_feature_registry = StrategyRegistry()
    no_feature_registry.register(trend)
    assert no_feature_registry.get(trend.strategy_id) is trend
    built = build_default_strategy_registry((trend,))
    assert built.get(trend.strategy_id, trend.strategy_version) is trend


@pytest.mark.parametrize(
    "factory",
    [
        lambda: TrendParameters(fast_period=4, slow_period=4),
        lambda: TrendParameters(fast_period=5, slow_period=2),
        lambda: BreakoutParameters(lookback=0),
        lambda: MomentumParameters(lookback=0),
        lambda: MomentumParameters(minimum_momentum=float("inf")),
    ],
)
def test_invalid_parameters_fail_early(factory) -> None:
    with pytest.raises(ValueError):
        factory()


def test_strategy_signal_identity_and_order_metadata_are_reproducible() -> None:
    strategy = EquityMomentumStrategy(MomentumParameters(lookback=2))
    bars = make_bars(["100", "100", "105"], asset_class=AssetClass.EQUITY, symbol="AAPL")
    first = strategy.on_bar(direct_context(strategy, bars, index=2))
    second = strategy.on_bar(direct_context(strategy, bars, index=2))
    assert first == second
    result, _ = run_strategy(strategy, bars)
    assert result.orders
    assert result.orders[-1].metadata["signal_id"] == result.orders[-1].order_id.rsplit("-", 1)[0]


def test_cost_aware_strategy_integration_remains_execution_model_agnostic() -> None:
    strategy = EquityMomentumStrategy(MomentumParameters(lookback=2))
    bars = make_bars(["100", "100", "105", "106"], asset_class=AssetClass.EQUITY, symbol="AAPL")
    zero, _ = run_strategy(strategy, bars)
    costly, _ = run_strategy(
        strategy,
        bars,
        spread=SpreadConfig(fallback_absolute=Decimal("1")),
        slippage=SlippageConfig(absolute=Decimal("1")),
        commission=CostConfig(per_unit=Decimal("1")),
    )
    assert costly.metrics.total_fees > zero.metrics.total_fees
    assert costly.equity_curve[-1].equity < zero.equity_curve[-1].equity


def test_backtest_adapter_rejects_signal_and_intent_provenance_mismatch() -> None:
    strategy = EquityMomentumStrategy(MomentumParameters(lookback=2))
    bars = make_bars(["100", "100", "105"], asset_class=AssetClass.EQUITY, symbol="AAPL")
    bar = bars[-1]
    backtest_context = BacktestContext(
        event_timestamp=bar.timestamp + bar.timeframe.duration,
        bar=bar,
        features={},
        cash=Decimal("1000"),
        equity=Decimal("1000"),
        positions=(),
    )
    original = strategy.on_bar
    valid = original(direct_context(strategy, bars, index=2))

    def wrong_signal(_context):
        return StrategyResult(
            replace(valid.signal, instrument=instrument(AssetClass.EQUITY, "MSFT"))
        )

    strategy.on_bar = wrong_signal
    with pytest.raises(StrategyConfigurationError):
        BacktestStrategyAdapter(strategy).on_event(backtest_context)

    def wrong_intent(_context):
        intent = OrderIntent(
            instrument=bar.instrument,
            side=OrderSide.BUY,
            quantity=Decimal("1"),
            signal_id="wrong-signal",
            strategy_id=strategy.strategy_id,
            strategy_version=strategy.strategy_version,
            reason="test",
        )
        return StrategyResult(valid.signal, (intent,))

    strategy.on_bar = wrong_intent
    with pytest.raises(StrategyConfigurationError):
        BacktestStrategyAdapter(strategy).on_event(backtest_context)


def test_remaining_rule_branches_and_parameter_validation_are_explicit() -> None:
    with pytest.raises(ValueError):
        TrendParameters(target_quantity=Decimal("0"))
    with pytest.raises(ValueError):
        TrendParameters(trend_strength_threshold=float("inf"))
    with pytest.raises(ValueError):
        BreakoutParameters(breakout_buffer=float("inf"))
    with pytest.raises(ValueError):
        TrendParameters(target_quantity=Decimal("NaN"))
    with pytest.raises(ValueError):
        BreakoutParameters(breakout_buffer=float("nan"))
    with pytest.raises(ValueError):
        TrendParameters(fast_period=2.0)

    trend = ForexTrendFollowingStrategy(
        TrendParameters(fast_period=2, slow_period=4, trend_strength_threshold=0.5)
    )
    trend_bars = make_bars(
        ["100", "101", "102", "103", "104"],
        asset_class=AssetClass.FOREX,
        symbol="EUR/USD",
    )
    trend_result = trend.on_bar(direct_context(trend, trend_bars, index=4))
    assert trend_result.signal.direction is SignalDirection.FLAT
    assert trend_result.signal.reason == "trend_below_threshold"

    descending = ForexTrendFollowingStrategy(TrendParameters(fast_period=2, slow_period=4))
    descending_bars = make_bars(
        ["104", "103", "102", "101", "100"],
        asset_class=AssetClass.FOREX,
        symbol="EUR/USD",
    )
    assert (
        descending.on_bar(direct_context(descending, descending_bars, index=4)).signal.direction
        is SignalDirection.SHORT
    )
    constant = make_bars(
        ["100", "100", "100", "100", "100"],
        asset_class=AssetClass.FOREX,
        symbol="EUR/USD",
    )
    neutral = ForexTrendFollowingStrategy(TrendParameters(fast_period=2, slow_period=4))
    assert (
        neutral.on_bar(direct_context(neutral, constant, index=4)).signal.reason == "ema_neutral"
    )

    breakout = ForexBreakoutStrategy(BreakoutParameters(lookback=2))
    breakout_bars = make_bars(
        ["100", "101", "100", "98"], asset_class=AssetClass.FOREX, symbol="EUR/USD"
    )
    assert (
        breakout.on_bar(direct_context(breakout, breakout_bars, index=3)).signal.direction
        is SignalDirection.SHORT
    )
    flat_bars = make_bars(
        ["100", "101", "101"], asset_class=AssetClass.FOREX, symbol="EUR/USD"
    )
    assert (
        breakout.on_bar(direct_context(breakout, flat_bars, index=2)).signal.direction
        is SignalDirection.FLAT
    )

    momentum = EquityMomentumStrategy(MomentumParameters(lookback=2, minimum_momentum=0.5))
    momentum_bars = make_bars(["100", "100", "105"], asset_class=AssetClass.EQUITY, symbol="AAPL")
    assert (
        momentum.on_bar(direct_context(momentum, momentum_bars, index=2)).signal.direction
        is SignalDirection.FLAT
    )
    equity_breakout = EquityBreakoutStrategy(BreakoutParameters(lookback=2))
    equity_flat_bars = make_bars(
        ["100", "101", "101"], asset_class=AssetClass.EQUITY, symbol="AAPL"
    )
    assert (
        equity_breakout.on_bar(direct_context(equity_breakout, equity_flat_bars, index=2))
        .signal.direction
        is SignalDirection.FLAT
    )

    base = RuleStrategy()
    with pytest.raises(NotImplementedError):
        _ = base.metadata
    with pytest.raises(NotImplementedError):
        _ = base.parameters


def test_context_signal_intent_and_metadata_validation() -> None:
    strategy = EquityMomentumStrategy(MomentumParameters(lookback=2))
    bars = make_bars(["100", "100", "105"], asset_class=AssetClass.EQUITY, symbol="AAPL")
    context = direct_context(strategy, bars, index=2)
    with pytest.raises(TypeError):
        context.parameters["new"] = 1  # type: ignore[index]

    with pytest.raises(ValueError):
        StrategyMetadata(
            strategy_id="bad",
            strategy_version="1",
            supported_asset_classes=frozenset({AssetClass.EQUITY}),
            supported_timeframes=frozenset({Timeframe.D1}),
            required_features=(),
            warmup_bars=0,
            parameter_schema=(),
            description=" ",
        )
    with pytest.raises(ValueError):
        StrategySignal(
            signal_id="bad",
            strategy_id=strategy.strategy_id,
            strategy_version=strategy.strategy_version,
            instrument=bars[0].instrument,
            timestamp=context.event_timestamp,
            timeframe=bars[0].timeframe,
            direction=SignalDirection.FLAT,
            reason="test",
            feature_provenance=(),
            score=float("inf"),
        )
    with pytest.raises(ValueError):
        StrategySignal(
            signal_id="bad",
            strategy_id=strategy.strategy_id,
            strategy_version=strategy.strategy_version,
            instrument=bars[0].instrument,
            timestamp=context.event_timestamp,
            timeframe=bars[0].timeframe,
            direction=SignalDirection.FLAT,
            reason="test",
            feature_provenance=(),
            confidence=2.0,
        )
    with pytest.raises(ValueError):
        OrderIntent(
            instrument=bars[0].instrument,
            side=OrderSide.BUY,
            quantity=Decimal("1"),
            signal_id="signal",
            strategy_id="test",
            strategy_version="1",
            reason="test",
            order_type=OrderType.LIMIT,
        )
    valid = OrderIntent(
        instrument=bars[0].instrument,
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        signal_id="signal",
        strategy_id="test",
        strategy_version="1",
        reason="test",
        order_type=OrderType.LIMIT,
        limit_price=Decimal("100"),
    )
    assert valid.limit_price == Decimal("100")


def test_registry_version_lookup_and_feature_scope_failures() -> None:
    feature_registry = build_default_registry()
    registry = StrategyRegistry(feature_registry)
    first = ForexTrendFollowingStrategy(TrendParameters(fast_period=2, slow_period=4))
    registry.register(first)

    second = ForexTrendFollowingStrategy(TrendParameters(fast_period=3, slow_period=5))
    second.strategy_version = "2"
    registry.register(second)
    with pytest.raises(StrategyRegistryError):
        registry.get(first.strategy_id)
    assert registry.get(first.strategy_id, "2") is second

    class MissingFeatureStrategy:
        strategy_id = "missing_feature"
        strategy_version = "1"
        parameters = {}
        metadata = StrategyMetadata(
            strategy_id="missing_feature",
            strategy_version="1",
            supported_asset_classes=frozenset({AssetClass.FOREX}),
            supported_timeframes=frozenset({Timeframe.H1}),
            required_features=(
                FeatureRequirement.from_parameters("not_registered", 1, {}),
            ),
            warmup_bars=1,
            parameter_schema=(),
            description="Missing feature test strategy.",
        )

        def on_bar(self, context):
            return context

    with pytest.raises(FeatureConfigurationError):
        registry.register(MissingFeatureStrategy())


def test_context_and_requirement_guards_reject_unsafe_inputs() -> None:
    strategy = EquityMomentumStrategy(MomentumParameters(lookback=2))
    bars = make_bars(["100", "100", "105"], asset_class=AssetClass.EQUITY, symbol="AAPL")
    context = direct_context(strategy, bars, index=2)
    requirement = FeatureRequirement.from_parameters("rolling_return", 1, {"window": 2})
    observation = feature_observations(strategy, bars)[-1]
    assert requirement.parameter_mapping == {"window": 2}
    assert requirement.matches(observation)
    assert not FeatureRequirement.from_parameters("other", 1, {}).matches(observation)

    with pytest.raises(ValueError):
        FeatureRequirement("", 1)
    with pytest.raises(ValueError):
        FeatureRequirement("return", 0)
    with pytest.raises(ValueError):
        FeatureRequirement("return", 1, (("window", 2), ("window", 3)))
    with pytest.raises(ValueError):
        FeatureRequirement("return", 1, (("", 2),))

    with pytest.raises(StrategyCausalityError):
        replace(context, event_timestamp=bars[2].timestamp)
    with pytest.raises(StrategyCausalityError):
        replace(context, position_quantity=Decimal("NaN"))
    with pytest.raises(StrategyCausalityError):
        replace(context, feature_observations=(observation.model_copy(
            update={"observation_timestamp": bars[2].timestamp + bars[2].timeframe.duration}
        ),))
    with pytest.raises(StrategyCausalityError):
        replace(context, feature_observations=(observation.model_copy(
            update={"availability_timestamp": context.event_timestamp + bars[2].timeframe.duration}
        ),))
    with pytest.raises(StrategyCausalityError):
        replace(
            context,
            feature_observations=(observation.model_copy(update={"timeframe": Timeframe.D1}),),
        )
    with pytest.raises(TimestampNormalizationError):
        replace(context, event_timestamp=datetime(2024, 1, 2))

    with pytest.raises(ValueError):
        StrategyMetadata(
            "", "1", frozenset({AssetClass.EQUITY}), frozenset({Timeframe.D1}), (), 0, (), "x"
        )
    with pytest.raises(ValueError):
        StrategyMetadata(
            "x", "", frozenset({AssetClass.EQUITY}), frozenset({Timeframe.D1}), (), 0, (), "x"
        )
    with pytest.raises(ValueError):
        StrategyMetadata("x", "1", frozenset(), frozenset({Timeframe.D1}), (), 0, (), "x")
    with pytest.raises(ValueError):
        StrategyMetadata("x", "1", frozenset({AssetClass.EQUITY}), frozenset(), (), 0, (), "x")
    with pytest.raises(ValueError):
        StrategyMetadata(
            "x", "1", frozenset({AssetClass.EQUITY}), frozenset({Timeframe.D1}), (), -1, (), "x"
        )
    with pytest.raises(ValueError):
        StrategyMetadata(
            "x",
            "1",
            frozenset({AssetClass.EQUITY}),
            frozenset({Timeframe.D1}),
            (),
            0,
            ("x", "x"),
            "x",
        )


def test_rule_context_scope_and_intent_price_guards() -> None:
    trend = ForexTrendFollowingStrategy(TrendParameters(fast_period=2, slow_period=4))
    forex_bars = make_bars(["100", "101"], asset_class=AssetClass.FOREX, symbol="EUR/USD")
    context = direct_context(trend, forex_bars, index=1)
    with pytest.raises(StrategyConfigurationError):
        trend._validate_context(replace(context, strategy_id="other"))
    with pytest.raises(StrategyConfigurationError):
        trend._validate_context(
            replace(
                context,
                bar=context.bar.model_copy(
                    update={"instrument": instrument(AssetClass.EQUITY, "AAPL")}
                ),
                feature_observations=(),
            )
        )
    equity = EquityMomentumStrategy(MomentumParameters(lookback=2))
    equity_bar = forex_bars[0].model_copy(
        update={"instrument": instrument(AssetClass.EQUITY, "AAPL"), "timeframe": Timeframe.M15}
    )
    equity_context = StrategyContext(
        event_timestamp=equity_bar.timestamp + equity_bar.timeframe.duration,
        bar=equity_bar,
        feature_observations=(),
        position_quantity=Decimal("0"),
        cash=Decimal("1000"),
        equity=Decimal("1000"),
        parameters=equity.parameters,
        strategy_id=equity.strategy_id,
        strategy_version=equity.strategy_version,
    )
    with pytest.raises(StrategyConfigurationError):
        equity._validate_context(equity_context)

    common = {
        "instrument": forex_bars[0].instrument,
        "side": OrderSide.BUY,
        "quantity": Decimal("1"),
        "signal_id": "signal",
        "strategy_id": "test",
        "strategy_version": "1",
        "reason": "test",
    }
    invalid = dict(common)
    invalid["quantity"] = Decimal("NaN")
    with pytest.raises(ValueError):
        OrderIntent(**invalid)
    invalid = dict(common)
    invalid["target_position"] = Decimal("NaN")
    with pytest.raises(ValueError):
        OrderIntent(**invalid)
    with pytest.raises(ValueError):
        OrderIntent(
            **common,
            order_type=OrderType.LIMIT,
            limit_price=Decimal("100"),
            stop_price=Decimal("99"),
        )
    with pytest.raises(ValueError):
        OrderIntent(**common, order_type=OrderType.STOP)
    with pytest.raises(ValueError):
        OrderIntent(
            **common,
            order_type=OrderType.STOP,
            stop_price=Decimal("100"),
            limit_price=Decimal("99"),
        )
    valid_stop = OrderIntent(
        **common,
        order_type=OrderType.STOP,
        stop_price=Decimal("100"),
    )
    assert valid_stop.stop_price == Decimal("100")
    with pytest.raises(ValueError):
        OrderIntent(**common, limit_price=Decimal("100"))
