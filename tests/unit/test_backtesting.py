"""Adversarial tests for the Phase 3 event-driven backtester."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from traderos.backtesting import (
    AccountingError,
    AlwaysFlatStrategy,
    BacktestConfig,
    BacktestConfigurationError,
    BacktestEngine,
    BuyAndHoldBenchmark,
    BuyAndHoldStrategy,
    CausalityViolation,
    CostConfig,
    ExecutionPolicyError,
    ExecutionSimulator,
    Fill,
    FixedOrderStrategy,
    FixedSlippageModel,
    InitialPosition,
    InsufficientCash,
    IntrabarAmbiguityPolicy,
    InvalidMarketDataError,
    InvalidOrderError,
    InvalidOrderTransition,
    MissingBarPolicy,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Portfolio,
    QuoteOrFixedSpreadModel,
    SlippageConfig,
    SpreadConfig,
    StaticCurrencyConverter,
    StrategyContext,
    TimeInForce,
)
from traderos.backtesting.metrics import calculate_metrics
from traderos.backtesting.models import EquitySnapshot
from traderos.data.bars import MarketBar
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.lineage import AdjustmentPolicy
from traderos.data.timeframes import Timeframe
from traderos.features import FeatureContext, FeatureEngine, FeatureRequest

BASE = datetime(2024, 1, 2, tzinfo=UTC)


def equity_instrument(symbol: str = "AAPL") -> Instrument:
    return Instrument(
        canonical_symbol=symbol,
        asset_class=AssetClass.EQUITY,
        exchange="NASDAQ",
        trading_currency="USD",
        provider_symbols={"fixture": symbol},
    )


def forex_instrument(symbol: str = "EUR/USD") -> Instrument:
    return Instrument(
        canonical_symbol=symbol,
        asset_class=AssetClass.FOREX,
        base_currency="EUR",
        quote_currency="USD",
        trading_currency="USD",
        provider_symbols={"fixture": symbol},
    )


def bars(
    opens: list[str],
    closes: list[str] | None = None,
    *,
    instrument: Instrument | None = None,
    highs: list[str] | None = None,
    lows: list[str] | None = None,
) -> list[MarketBar]:
    closes = closes or opens
    highs = highs or [
        str(max(Decimal(open_), Decimal(close)) + Decimal("1"))
        for open_, close in zip(opens, closes, strict=True)
    ]
    lows = lows or [
        str(min(Decimal(open_), Decimal(close)) - Decimal("1"))
        for open_, close in zip(opens, closes, strict=True)
    ]
    instrument = instrument or equity_instrument()
    return [
        MarketBar(
            instrument=instrument,
            timeframe=Timeframe.H1,
            adjustment_policy=AdjustmentPolicy.RAW,
            timestamp=BASE + timedelta(hours=index),
            open=Decimal(open_),
            high=Decimal(highs[index]),
            low=Decimal(lows[index]),
            close=Decimal(close),
            volume=Decimal("1000"),
            source="fixture",
            currency=instrument.trading_currency,
            ingestion_timestamp=BASE,
        )
        for index, (open_, close) in enumerate(zip(opens, closes, strict=True))
    ]


def config(
    *,
    symbols: tuple[str, ...] = ("AAPL",),
    strategy_id: str = "fixed",
    strategy_version: str = "1",
    end_hours: int = 5,
    starting_cash: str = "1000",
    commission: CostConfig | None = None,
    spread: SpreadConfig | None = None,
    slippage: SlippageConfig | None = None,
    ambiguity: IntrabarAmbiguityPolicy = IntrabarAmbiguityPolicy.REJECT,
    initial_positions: tuple[InitialPosition, ...] = (),
    missing_bar_policy: MissingBarPolicy = MissingBarPolicy.ALLOW,
    max_fill_quantity: Decimal | None = None,
    feature_versions: tuple[tuple[str, int], ...] = (),
    annualization_factor: int | None = None,
) -> BacktestConfig:
    return BacktestConfig(
        dataset_version="fixture-v1",
        instrument_symbols=symbols,
        timeframe=Timeframe.H1,
        start=BASE,
        end=BASE + timedelta(hours=end_hours),
        starting_cash=Decimal(starting_cash),
        account_currency="USD",
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        commission=commission or CostConfig(),
        spread=spread or SpreadConfig(),
        slippage=slippage or SlippageConfig(),
        ambiguity_policy=ambiguity,
        initial_positions=initial_positions,
        missing_bar_policy=missing_bar_policy,
        max_fill_quantity=max_fill_quantity,
        feature_versions=feature_versions,
        annualization_factor=annualization_factor,
    )


def run_fixed(
    market_bars: list[MarketBar],
    orders_by_event: dict[datetime, tuple[Order, ...]],
    *,
    backtest_config: BacktestConfig | None = None,
):
    backtest_config = backtest_config or config(end_hours=len(market_bars) + 1)
    return BacktestEngine(backtest_config).run(
        market_bars,
        FixedOrderStrategy(
            orders_by_event,
            strategy_id=backtest_config.strategy_id,
            strategy_version=backtest_config.strategy_version,
        ),
    )


def order(
    market_bar: MarketBar,
    order_id: str,
    side: OrderSide,
    quantity: str,
    *,
    order_type: OrderType = OrderType.MARKET,
    limit_price: str | None = None,
    stop_price: str | None = None,
    time_in_force: TimeInForce = TimeInForce.GTC,
) -> Order:
    return Order(
        order_id=order_id,
        instrument=market_bar.instrument,
        side=side,
        quantity=Decimal(quantity),
        order_type=order_type,
        limit_price=None if limit_price is None else Decimal(limit_price),
        stop_price=None if stop_price is None else Decimal(stop_price),
        time_in_force=time_in_force,
    )


def test_buy_and_hold_uses_next_bar_open_and_never_same_bar_fill() -> None:
    market_bars = bars(["100", "100", "110"], ["120", "110", "120"])
    result = BacktestEngine(
        config(strategy_id="buy_and_hold", end_hours=4, starting_cash="1000")
    ).run(
        market_bars,
        BuyAndHoldStrategy({"AAPL": Decimal("5")}),
    )

    assert len(result.fills) == 1
    assert result.fills[0].timestamp == market_bars[1].timestamp
    assert result.fills[0].price == Decimal("100")
    assert result.equity_curve[0].equity == Decimal("1000")
    assert result.equity_curve[1].equity == Decimal("1050")
    assert result.equity_curve[2].equity == Decimal("1100")


def test_market_spread_slippage_and_commission_reduce_equity() -> None:
    market_bars = bars(["100", "100", "110"], ["100", "100", "110"])
    zero = BacktestEngine(config(strategy_id="buy_and_hold", end_hours=4)).run(
        market_bars,
        BuyAndHoldStrategy({"AAPL": Decimal("5")}),
    )
    costly_config = config(
        strategy_id="buy_and_hold",
        end_hours=4,
        commission=CostConfig(per_unit=Decimal("1")),
        spread=SpreadConfig(fallback_absolute=Decimal("2")),
        slippage=SlippageConfig(absolute=Decimal("1")),
    )
    costly = BacktestEngine(costly_config).run(
        market_bars,
        BuyAndHoldStrategy({"AAPL": Decimal("5")}),
    )

    assert costly.fills[0].price == Decimal("102")
    assert costly.metrics.total_fees == Decimal("5")
    assert costly.equity_curve[-1].equity < zero.equity_curve[-1].equity


def test_limit_order_and_stop_gap_use_explicit_prices() -> None:
    market_bars = bars(
        ["100", "100", "90"],
        ["100", "100", "90"],
        highs=["101", "110", "95"],
        lows=["99", "90", "80"],
    )
    limit = order(
        market_bars[0],
        "limit",
        OrderSide.BUY,
        "1",
        order_type=OrderType.LIMIT,
        limit_price="95",
    )
    stop = order(
        market_bars[0],
        "stop",
        OrderSide.SELL,
        "1",
        order_type=OrderType.STOP,
        stop_price="100",
    )
    result = run_fixed(
        market_bars,
        {BASE + timedelta(hours=1): (limit,), BASE + timedelta(hours=2): (stop,)},
        backtest_config=config(end_hours=4, starting_cash="1000"),
    )

    assert result.fills[0].price == Decimal("95")
    assert result.fills[1].price == Decimal("90")
    assert result.fills[1].price != Decimal("100")


def test_ambiguous_conditional_orders_are_rejected_by_default() -> None:
    market_bars = bars(
        ["100", "100"],
        ["100", "100"],
        highs=["101", "110"],
        lows=["99", "90"],
    )
    buy_limit = order(
        market_bars[0], "a-limit", OrderSide.BUY, "1", order_type=OrderType.LIMIT, limit_price="95"
    )
    sell_limit = order(
        market_bars[0],
        "b-limit",
        OrderSide.SELL,
        "1",
        order_type=OrderType.LIMIT,
        limit_price="105",
    )
    result = run_fixed(
        market_bars,
        {BASE + timedelta(hours=1): (buy_limit, sell_limit)},
        backtest_config=config(end_hours=3),
    )

    assert result.fills == ()
    assert buy_limit.status is OrderStatus.REJECTED
    assert sell_limit.status is OrderStatus.REJECTED
    assert len(result.rejected_orders) == 2


def test_position_reversal_realizes_pnl_and_opens_new_side() -> None:
    market_bars = bars(["100", "100", "110"], ["100", "100", "110"])
    buy = order(market_bars[0], "buy", OrderSide.BUY, "10")
    sell = order(market_bars[0], "sell", OrderSide.SELL, "15")
    result = run_fixed(
        market_bars,
        {BASE + timedelta(hours=1): (buy,), BASE + timedelta(hours=2): (sell,)},
        backtest_config=config(end_hours=4, starting_cash="2000"),
    )

    assert len(result.trades) == 1
    assert result.trades[0].gross_pnl == Decimal("100")
    final_position = result.equity_curve[-1].positions[0]
    assert final_position.quantity == Decimal("-5")
    assert final_position.average_entry_price == Decimal("110")
    assert result.equity_curve[-1].equity == Decimal("2100")


def test_flat_portfolio_has_no_changes_or_fees() -> None:
    market_bars = bars(["100", "101", "102"])
    result = BacktestEngine(config(strategy_id="always_flat", end_hours=4)).run(
        market_bars,
        AlwaysFlatStrategy(),
    )
    assert result.fills == ()
    assert result.trades == ()
    assert all(snapshot.cash == Decimal("1000") for snapshot in result.equity_curve)
    assert all(snapshot.equity == Decimal("1000") for snapshot in result.equity_curve)
    assert result.metrics.total_fees == Decimal("0")


def test_multi_asset_state_isolated_and_input_is_explicitly_normalized() -> None:
    aapl = bars(["100", "100", "105"], ["100", "100", "105"])
    msft = bars(
        ["200", "200", "210"],
        ["200", "200", "210"],
        instrument=equity_instrument("MSFT"),
    )
    combined = [aapl[0], msft[0], msft[1], aapl[1], aapl[2], msft[2]]
    backtest_config = config(
        symbols=("AAPL", "MSFT"),
        strategy_id="buy_and_hold",
        end_hours=4,
        starting_cash="5000",
    )
    result = BacktestEngine(backtest_config).run(
        combined,
        BuyAndHoldStrategy({"AAPL": Decimal("5"), "MSFT": Decimal("2")}),
    )

    assert {snapshot.symbol for snapshot in result.equity_curve[-1].positions} == {"AAPL", "MSFT"}
    assert {fill.instrument.canonical_symbol for fill in result.fills} == {"AAPL", "MSFT"}


def test_fx_quote_currency_conversion_is_explicit() -> None:
    instrument = forex_instrument()
    market_bars = bars(
        ["1.10", "1.10", "1.20"],
        ["1.10", "1.10", "1.20"],
        instrument=instrument,
    )
    strategy = BuyAndHoldStrategy({"EUR/USD": Decimal("1000")})
    result = BacktestEngine(
        config(
            symbols=("EUR/USD",),
            strategy_id="buy_and_hold",
            end_hours=4,
            starting_cash="2000",
        )
    ).run(market_bars, strategy)

    assert result.equity_curve[-1].equity == Decimal("2100")


def test_feature_visibility_respects_phase2_availability() -> None:
    market_bars = bars(["100", "101", "102"])
    feature_set = FeatureEngine().compute_feature_set(
        market_bars,
        [FeatureRequest(name="simple_return", version=1)],
        FeatureContext(
            instrument=market_bars[0].instrument,
            timeframe=Timeframe.H1,
            source_dataset_version="fixture-v1",
            computation_timestamp=BASE,
        ),
    )

    @dataclass
    class Probe:
        strategy_id: str = "probe"
        strategy_version: str = "1"
        contexts: list[StrategyContext] = field(default_factory=list)

        def on_event(self, context: StrategyContext) -> tuple[Order, ...]:
            self.contexts.append(context)
            return ()

    probe = Probe()
    BacktestEngine(config(strategy_id="probe", end_hours=4)).run(
        market_bars, probe, features=feature_set.observations
    )
    assert probe.contexts[0].features[("simple_return", 1)] is None
    assert probe.contexts[1].features[("simple_return", 1)] == pytest.approx(0.01)


def test_future_append_and_future_mutation_do_not_change_prior_replay() -> None:
    market_bars = bars(["100", "100", "105", "106", "107"])
    backtest_config = config(end_hours=6)
    first = BacktestEngine(backtest_config).run(
        market_bars[:3], AlwaysFlatStrategy(strategy_id="fixed")
    )
    appended = BacktestEngine(backtest_config).run(
        market_bars, AlwaysFlatStrategy(strategy_id="fixed")
    )
    mutated = list(market_bars)
    mutated[4] = mutated[4].model_copy(update={"close": Decimal("999")})
    changed = BacktestEngine(backtest_config).run(mutated, AlwaysFlatStrategy(strategy_id="fixed"))

    assert appended.equity_curve[:3] == first.equity_curve
    assert changed.equity_curve[:4] == appended.equity_curve[:4]


def test_missing_bars_are_not_synthesized_and_duplicates_fail() -> None:
    market_bars = bars(["100", "101", "102"])
    gap = [market_bars[0], market_bars[2]]
    result = BacktestEngine(config(strategy_id="always_flat", end_hours=4)).run(
        gap, AlwaysFlatStrategy()
    )
    assert result.event_count == 2
    assert [snapshot.timestamp for snapshot in result.equity_curve] == [
        BASE + timedelta(hours=1),
        BASE + timedelta(hours=3),
    ]
    with pytest.raises(InvalidMarketDataError):
        BacktestEngine(config(strategy_id="always_flat", end_hours=4)).run(
            [market_bars[0], market_bars[0]], AlwaysFlatStrategy()
        )


def test_order_state_machine_rejects_impossible_transition() -> None:
    market_bar = bars(["100"])[0]
    item = order(market_bar, "state", OrderSide.BUY, "1")
    item.transition(OrderStatus.SUBMITTED)
    item.transition(OrderStatus.ACCEPTED)
    item.transition(OrderStatus.FILLED)
    with pytest.raises(InvalidOrderTransition):
        item.transition(OrderStatus.CREATED)


def test_experiment_identity_changes_with_assumptions() -> None:
    base = config()
    changed = config(spread=SpreadConfig(fallback_absolute=Decimal("0.1")))
    assert base.experiment_id != changed.experiment_id
    assert base.experiment_id == config().experiment_id


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"order_id": "", "quantity": Decimal("1")}, "order_id"),
        ({"order_id": "bad", "quantity": Decimal("0")}, "quantity"),
        (
            {"order_id": "bad", "quantity": Decimal("1"), "order_type": OrderType.LIMIT},
            "limit_price",
        ),
        (
            {
                "order_id": "bad",
                "quantity": Decimal("1"),
                "order_type": OrderType.MARKET,
                "limit_price": Decimal("1"),
            },
            "only limit",
        ),
        (
            {"order_id": "bad", "quantity": Decimal("1"), "order_type": OrderType.STOP},
            "stop_price",
        ),
        (
            {
                "order_id": "bad",
                "quantity": Decimal("1"),
                "order_type": OrderType.MARKET,
                "stop_price": Decimal("1"),
            },
            "only stop",
        ),
    ],
)
def test_order_constructor_rejects_invalid_contract(
    kwargs: dict[str, object], message: str
) -> None:
    market_bar = bars(["100"])[0]
    with pytest.raises(InvalidOrderError, match=message):
        Order(
            instrument=market_bar.instrument,
            side=OrderSide.BUY,
            **kwargs,
        )


def test_order_partial_fill_and_terminal_states_are_explicit() -> None:
    item = order(bars(["100"])[0], "partial", OrderSide.BUY, "2")
    item.transition(OrderStatus.SUBMITTED)
    item.transition(OrderStatus.ACCEPTED)
    item.record_fill(Decimal("1"))
    assert item.status is OrderStatus.PARTIALLY_FILLED
    assert item.remaining_quantity == Decimal("1")
    item.record_fill(Decimal("1"))
    assert item.status is OrderStatus.FILLED
    with pytest.raises(InvalidOrderError):
        item.record_fill(Decimal("1"))


def test_execution_uses_quotes_and_adverse_slippage_for_both_sides() -> None:
    market_bar = bars(["100"])[0].model_copy(update={"bid": Decimal("99"), "ask": Decimal("101")})
    buy = order(market_bar, "buy", OrderSide.BUY, "1")
    sell = order(market_bar, "sell", OrderSide.SELL, "1")
    for item in (buy, sell):
        item.transition(OrderStatus.SUBMITTED)
        item.transition(OrderStatus.ACCEPTED)
    simulator = ExecutionSimulator(slippage_model=FixedSlippageModel(Decimal("0.5")))
    report = simulator.process((buy, sell), market_bar, fill_timestamp=BASE)
    assert [fill.price for fill in report.fills] == [Decimal("101.5"), Decimal("98.5")]
    assert report.fills[0].slippage == Decimal("0.5")
    assert report.fills[1].slippage == Decimal("0.5")


def test_execution_supports_partial_liquidity_and_ioc_expiry() -> None:
    market_bar = bars(["100", "101"])[0]
    partial = order(market_bar, "partial", OrderSide.BUY, "3")
    partial.transition(OrderStatus.SUBMITTED)
    partial.transition(OrderStatus.ACCEPTED)
    simulator = ExecutionSimulator(max_fill_quantity=Decimal("1"))
    first = simulator.process((partial,), market_bar, fill_timestamp=BASE)
    second = simulator.process((partial,), market_bar, fill_timestamp=BASE + timedelta(hours=1))
    assert first.fills[0].quantity == Decimal("1")
    assert second.fills[0].quantity == Decimal("1")
    assert partial.status is OrderStatus.PARTIALLY_FILLED

    ioc = order(
        market_bar,
        "ioc",
        OrderSide.BUY,
        "1",
        order_type=OrderType.LIMIT,
        limit_price="90",
        time_in_force=TimeInForce.IOC,
    )
    ioc.transition(OrderStatus.SUBMITTED)
    ioc.transition(OrderStatus.ACCEPTED)
    no_fill = simulator.process((ioc,), market_bar, fill_timestamp=BASE)
    assert no_fill.fills == ()
    assert ioc.status is OrderStatus.EXPIRED


def test_ambiguity_policy_can_choose_deterministically() -> None:
    market_bars = bars(["100", "100"], highs=["101", "110"], lows=["99", "90"])
    first = order(
        market_bars[0], "b-limit", OrderSide.BUY, "1", order_type=OrderType.LIMIT, limit_price="95"
    )
    second = order(
        market_bars[0],
        "a-limit",
        OrderSide.SELL,
        "1",
        order_type=OrderType.LIMIT,
        limit_price="105",
    )
    result = run_fixed(
        market_bars,
        {BASE + timedelta(hours=1): (first, second)},
        backtest_config=config(
            end_hours=3,
            ambiguity=IntrabarAmbiguityPolicy.FIRST_ORDER_ID,
            starting_cash="1000",
        ),
    )
    assert [fill.order_id for fill in result.fills] == ["a-limit"]


def test_entry_and_exit_fees_are_allocated_to_closed_trade() -> None:
    market_bars = bars(["100", "100", "110"])
    buy = order(market_bars[0], "buy", OrderSide.BUY, "10")
    sell = order(market_bars[0], "sell", OrderSide.SELL, "10")
    result = run_fixed(
        market_bars,
        {BASE + timedelta(hours=1): (buy,), BASE + timedelta(hours=2): (sell,)},
        backtest_config=config(
            end_hours=4,
            starting_cash="2000",
            commission=CostConfig(per_unit=Decimal("1")),
        ),
    )
    assert result.trades[0].gross_pnl == Decimal("100")
    assert result.trades[0].fees == Decimal("20")
    assert result.trades[0].net_pnl == Decimal("80")
    assert result.metrics.total_fees == Decimal("20")
    assert result.equity_curve[-1].equity == Decimal("2080")


def test_initial_position_and_insufficient_cash_are_explicit() -> None:
    market_bars = bars(["100", "100"])
    result = BacktestEngine(
        config(
            strategy_id="always_flat",
            end_hours=3,
            starting_cash="1000",
            initial_positions=(InitialPosition(symbol="AAPL", quantity=Decimal("5")),),
        )
    ).run(market_bars, AlwaysFlatStrategy())
    assert result.equity_curve[0].positions[0].quantity == Decimal("5")
    assert result.equity_curve[0].equity == Decimal("1000")

    portfolio = Portfolio(starting_cash=Decimal("10"), account_currency="USD")
    with pytest.raises(InsufficientCash):
        portfolio.seed_position(market_bars[0].instrument, Decimal("1"), Decimal("100"), BASE)


def test_engine_rejects_empty_data_and_requires_calendar_for_gap_rejection() -> None:
    with pytest.raises(InvalidMarketDataError):
        BacktestEngine(config()).run([], AlwaysFlatStrategy(strategy_id="fixed"))
    with pytest.raises(BacktestConfigurationError):
        BacktestEngine(config(missing_bar_policy=MissingBarPolicy.REJECT))


def test_calendar_gap_rejection_does_not_synthesize_bars() -> None:
    class FixtureCalendar:
        calendar_id = "fixture"

        def is_open_at(self, timestamp: datetime) -> bool:
            return True

        def expected_bar_timestamps(
            self, start: datetime, end: datetime, timeframe: Timeframe
        ) -> tuple[datetime, ...]:
            return tuple(
                start + timeframe.duration * index
                for index in range(int((end - start) / timeframe.duration))
            )

    market_bars = bars(["100", "101", "102"])
    with pytest.raises(InvalidMarketDataError, match="missing calendar bars"):
        BacktestEngine(
            config(
                strategy_id="always_flat",
                end_hours=4,
                missing_bar_policy=MissingBarPolicy.REJECT,
            ),
            calendar=FixtureCalendar(),
        ).run(market_bars[:2], AlwaysFlatStrategy())


def test_engine_rejects_market_data_contract_mismatches() -> None:
    market_bar = bars(["100"])[0]
    cases = [
        [market_bar.model_copy(update={"instrument": equity_instrument("MSFT")})],
        [market_bar.model_copy(update={"timeframe": Timeframe.H4})],
        [market_bar.model_copy(update={"adjustment_policy": AdjustmentPolicy.ADJUSTED})],
        [market_bar.model_copy(update={"timestamp": BASE - timedelta(hours=1)})],
    ]
    for invalid in cases:
        with pytest.raises(InvalidMarketDataError):
            BacktestEngine(config(strategy_id="always_flat", end_hours=2)).run(
                invalid, AlwaysFlatStrategy()
            )


def test_engine_rejects_bad_features_and_strategy_orders() -> None:
    market_bars = bars(["100", "101", "102"])
    feature_set = FeatureEngine().compute_feature_set(
        market_bars,
        [FeatureRequest(name="simple_return", version=1)],
        FeatureContext(
            instrument=market_bars[0].instrument,
            timeframe=Timeframe.H1,
            source_dataset_version="fixture-v1",
            computation_timestamp=BASE,
        ),
    )
    bad_lineage = feature_set.observations[0].lineage.model_copy(
        update={"source_dataset_version": "other-dataset"}
    )
    bad_observation = feature_set.observations[0].model_copy(update={"lineage": bad_lineage})
    with pytest.raises(CausalityViolation):
        BacktestEngine(
            config(
                strategy_id="always_flat",
                end_hours=4,
                feature_versions=(("simple_return", 1),),
            )
        ).run(market_bars, AlwaysFlatStrategy(), features=(bad_observation,))

    @dataclass
    class BadStrategy:
        strategy_id: str = "fixed"
        strategy_version: str = "1"

        def on_event(self, context: StrategyContext) -> tuple[Order, ...]:
            return (
                Order(
                    order_id="wrong-symbol",
                    instrument=equity_instrument("MSFT"),
                    side=OrderSide.BUY,
                    quantity=Decimal("1"),
                ),
            )

    result = BacktestEngine(config(strategy_id="fixed", end_hours=2)).run(
        market_bars[:1], BadStrategy()
    )
    assert result.orders[0].status is OrderStatus.REJECTED
    assert result.rejected_orders


def test_engine_wraps_strategy_failure_and_expires_pending_orders() -> None:
    @dataclass
    class ExplodingStrategy:
        strategy_id: str = "explode"
        strategy_version: str = "1"

        def on_event(self, context: StrategyContext) -> tuple[Order, ...]:
            del context
            raise RuntimeError("unexpected")

    with pytest.raises(BacktestConfigurationError, match="strategy failed"):
        BacktestEngine(config(strategy_id="explode", end_hours=2)).run(
            bars(["100"]), ExplodingStrategy()
        )

    market_bars = bars(["100", "100"])
    pending = order(market_bars[0], "pending", OrderSide.BUY, "1")
    result = run_fixed(
        market_bars[:1],
        {BASE + timedelta(hours=1): (pending,)},
        backtest_config=config(end_hours=2),
    )
    assert pending.status is OrderStatus.EXPIRED
    assert "pending:end_of_data" in result.rejected_orders


def test_metrics_and_benchmark_handle_empty_and_drawdown_cases() -> None:
    empty = calculate_metrics(
        (), (), starting_equity=Decimal("100"), annualization_factor=None, total_fees=Decimal("0")
    )
    assert empty.total_return is None
    curve = tuple(
        EquitySnapshot(
            timestamp=BASE + timedelta(days=index),
            cash=Decimal(str(value)),
            gross_market_value=Decimal("0"),
            net_market_value=Decimal("0"),
            equity=Decimal(str(value)),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            fees=Decimal("0"),
            gross_exposure=Decimal("0"),
            positions=(),
        )
        for index, value in enumerate((100, 90, 110))
    )
    metrics = calculate_metrics(
        curve,
        (),
        starting_equity=Decimal("100"),
        annualization_factor=2,
        turnover_notional=100,
        total_fees=Decimal("1"),
    )
    assert metrics.maximum_drawdown == pytest.approx(-0.1)
    assert metrics.periodic_returns == pytest.approx((-0.1, 110 / 90 - 1))
    assert metrics.recovery_time == timedelta(days=1)
    assert BuyAndHoldBenchmark("AAPL").equity_curve(bars(["100", "110"]), Decimal("100")) == (
        100.0,
        110.0,
    )
    assert BuyAndHoldBenchmark("MSFT").equity_curve(bars(["100"]), Decimal("100")) == ()


def test_currency_conversion_and_accounting_errors_are_explicit() -> None:
    converter = StaticCurrencyConverter({("EUR", "USD"): Decimal("1.1")})
    assert converter.convert(Decimal("10"), "EUR", "USD", BASE) == Decimal("11.0")
    assert converter.convert(Decimal("11"), "USD", "EUR", BASE) == Decimal("10")
    with pytest.raises(AccountingError):
        converter.convert(Decimal("1"), "GBP", "USD", BASE)
    portfolio = Portfolio(starting_cash=Decimal("100"), account_currency="USD")
    with pytest.raises(AccountingError):
        portfolio.convert_to_account(Decimal("1"), None, BASE)


def test_spread_and_slippage_reject_invalid_configuration() -> None:
    with pytest.raises(ExecutionPolicyError):
        FixedSlippageModel(Decimal("-1")).apply(OrderSide.BUY, Decimal("10"))
    with pytest.raises(ValueError):
        CostConfig(rate=Decimal("-1"))
    with pytest.raises(ValueError):
        SpreadConfig(fallback_absolute=Decimal("-1"))
    with pytest.raises(ValueError):
        SlippageConfig(absolute=Decimal("-1"))


def test_configuration_validation_rejects_ambiguous_or_unsafe_values() -> None:
    with pytest.raises(ValueError):
        BacktestConfig(
            **{
                **config().model_dump(),
                "start": BASE + timedelta(hours=2),
                "end": BASE,
            }
        )
    with pytest.raises(ValueError):
        BacktestConfig(
            dataset_version="v1",
            instrument_symbols=("AAPL", "AAPL"),
            timeframe=Timeframe.H1,
            start=BASE,
            end=BASE + timedelta(hours=1),
            starting_cash=Decimal("1"),
            account_currency="USD",
            strategy_id="x",
            strategy_version="1",
        )
    with pytest.raises(ValueError):
        BacktestConfig(**{**config().model_dump(), "annualization_factor": 0})
    with pytest.raises(ValueError):
        BacktestConfig(**{**config().model_dump(), "max_fill_quantity": Decimal("0")})
    with pytest.raises(ValueError):
        config(
            initial_positions=(InitialPosition(symbol="MSFT", quantity=Decimal("1")),)
        )


def test_engine_rejects_execution_and_strategy_version_mismatches() -> None:
    invalid_execution_config = config().model_copy(
        update={"execution_policy": "unsupported"}
    )
    with pytest.raises(BacktestConfigurationError):
        BacktestEngine(invalid_execution_config)
    with pytest.raises(BacktestConfigurationError):
        BacktestEngine(config(strategy_id="fixed")).run(
            bars(["100"]), AlwaysFlatStrategy(strategy_id="other")
        )
    with pytest.raises(BacktestConfigurationError):
        BacktestEngine(config(strategy_id="always_flat", strategy_version="2")).run(
            bars(["100"]), AlwaysFlatStrategy()
        )


def test_engine_rejects_calendar_closed_events_and_accepts_complete_calendar() -> None:
    class Calendar:
        calendar_id = "fixture"

        def is_open_at(self, timestamp: datetime) -> bool:
            return timestamp != BASE

        def expected_bar_timestamps(
            self, start: datetime, end: datetime, timeframe: Timeframe
        ) -> tuple[datetime, ...]:
            return tuple(
                start + timeframe.duration * index
                for index in range(int((end - start) / timeframe.duration))
            )

    with pytest.raises(InvalidMarketDataError, match="outside"):
        BacktestEngine(config(strategy_id="always_flat"), calendar=Calendar()).run(
            bars(["100"]), AlwaysFlatStrategy()
        )
    complete = bars(["100", "101", "102", "103"])
    open_calendar = type(
        "OpenCalendar",
        (),
        {
            "calendar_id": "open",
            "is_open_at": lambda self, timestamp: True,
            "expected_bar_timestamps": Calendar.expected_bar_timestamps,
        },
    )()
    result = BacktestEngine(
        config(strategy_id="always_flat", end_hours=4, missing_bar_policy=MissingBarPolicy.REJECT),
        calendar=open_calendar,
    ).run(complete, AlwaysFlatStrategy())
    assert result.event_count == 4


def test_feature_validation_rejects_scope_duplicates_and_missing_versions() -> None:
    market_bars = bars(["100", "101", "102"])
    feature_set = FeatureEngine().compute_feature_set(
        market_bars,
        [FeatureRequest(name="simple_return", version=1)],
        FeatureContext(
            instrument=market_bars[0].instrument,
            timeframe=Timeframe.H1,
            source_dataset_version="fixture-v1",
            computation_timestamp=BASE,
        ),
    )
    observation = feature_set.observations[0]
    with pytest.raises(CausalityViolation):
        BacktestEngine(
            config(
                strategy_id="always_flat",
                end_hours=4,
                feature_versions=(("simple_return", 1),),
            )
        ).run(market_bars, AlwaysFlatStrategy(), features=(observation, observation))
    wrong_symbol = observation.model_copy(
        update={"instrument": equity_instrument("MSFT")}
    )
    with pytest.raises(CausalityViolation):
        BacktestEngine(config(strategy_id="always_flat", end_hours=4)).run(
            market_bars, AlwaysFlatStrategy(), features=(wrong_symbol,)
        )
    wrong_timeframe = observation.model_copy(update={"timeframe": Timeframe.H4})
    with pytest.raises(CausalityViolation):
        BacktestEngine(config(strategy_id="always_flat", end_hours=4)).run(
            market_bars, AlwaysFlatStrategy(), features=(wrong_timeframe,)
        )
    wrong_adjustment = observation.lineage.model_copy(
        update={"adjustment_policy": AdjustmentPolicy.ADJUSTED}
    )
    with pytest.raises(CausalityViolation):
        BacktestEngine(config(strategy_id="always_flat", end_hours=4)).run(
            market_bars,
            AlwaysFlatStrategy(),
            features=(observation.model_copy(update={"lineage": wrong_adjustment}),),
        )
    with pytest.raises(CausalityViolation):
        BacktestEngine(
            config(
                strategy_id="always_flat",
                end_hours=4,
                feature_versions=(("other_feature", 1),),
            )
        ).run(market_bars, AlwaysFlatStrategy(), features=(observation,))
    with pytest.raises(BacktestConfigurationError):
        BacktestEngine(
            config(
                strategy_id="always_flat",
                end_hours=4,
                feature_versions=(("simple_return", 1), ("other_feature", 1)),
            )
        ).run(market_bars, AlwaysFlatStrategy(), features=(observation,))


def test_day_orders_expire_and_cross_asset_orders_wait_for_their_event() -> None:
    first = bars(["100", "100"])
    day_bars = [
        first[0],
        first[1],
        first[1].model_copy(update={"timestamp": BASE + timedelta(days=1)}),
    ]
    day_order = order(day_bars[0], "day", OrderSide.BUY, "1", time_in_force=TimeInForce.DAY)
    result = run_fixed(
        day_bars,
        {BASE + timedelta(hours=2): (day_order,)},
        backtest_config=config(end_hours=26),
    )
    assert day_order.status is OrderStatus.EXPIRED
    assert day_order.rejection_reason == "day_order_expired"
    assert result.fills == ()



def test_bad_order_metadata_and_duplicate_ids_are_rejected() -> None:
    market_bar = bars(["100"])[0]

    @dataclass
    class BadOrders:
        mode: str
        strategy_id: str = "bad"
        strategy_version: str = "1"

        def on_event(self, context: StrategyContext) -> tuple[Order, ...]:
            item = order(context.bar, self.mode, OrderSide.BUY, "1")
            if self.mode == "status":
                item.transition(OrderStatus.SUBMITTED)
                item.transition(OrderStatus.ACCEPTED)
            elif self.mode == "created":
                item.created_timestamp = BASE
            elif self.mode == "submitted":
                item.submitted_timestamp = BASE
            elif self.mode == "metadata":
                item.strategy_id = "other"
            return (item,)

    for mode in ("status", "created", "submitted", "metadata"):
        result = BacktestEngine(config(strategy_id="bad", end_hours=2)).run(
            [market_bar], BadOrders(mode)
        )
        assert result.orders[0].status in {OrderStatus.REJECTED, OrderStatus.ACCEPTED}
        assert result.rejected_orders

    @dataclass
    class DuplicateOrders:
        strategy_id: str = "duplicate"
        strategy_version: str = "1"

        def on_event(self, context: StrategyContext) -> tuple[Order, ...]:
            return (order(context.bar, "same", OrderSide.BUY, "1"),)

    duplicate_bars = bars(["100", "100"])
    duplicate_result = BacktestEngine(config(strategy_id="duplicate", end_hours=3)).run(
        duplicate_bars, DuplicateOrders()
    )
    assert any("duplicate order_id" in message for message in duplicate_result.rejected_orders)


def _fill(
    market_bar: MarketBar,
    fill_id: str,
    side: OrderSide,
    quantity: str,
    price: str,
    *,
    commission: str = "0",
) -> Fill:
    return Fill(
        fill_id=fill_id,
        order_id=fill_id,
        instrument=market_bar.instrument,
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        reference_price=Decimal(price),
        slippage=Decimal("0"),
        commission=Decimal(commission),
        commission_currency="USD",
        timestamp=BASE,
        strategy_id="test",
        strategy_version="1",
    )


def test_engine_reschedules_partial_fills_and_features_respect_visibility_filters() -> None:
    market_bars = bars(["100", "100", "100"])
    buy = order(market_bars[0], "partial-engine", OrderSide.BUY, "3")
    partial_result = run_fixed(
        market_bars,
        {BASE + timedelta(hours=1): (buy,)},
        backtest_config=config(end_hours=4, max_fill_quantity=Decimal("1")),
    )
    assert [fill.quantity for fill in partial_result.fills] == [Decimal("1"), Decimal("1")]
    assert buy.status is OrderStatus.EXPIRED
    assert buy.filled_quantity == Decimal("2")

    feature_set = FeatureEngine().compute_feature_set(
        market_bars,
        [FeatureRequest(name="simple_return", version=1)],
        FeatureContext(
            instrument=market_bars[0].instrument,
            timeframe=Timeframe.H1,
            source_dataset_version="fixture-v1",
            computation_timestamp=BASE,
        ),
    )
    latest = feature_set.observations[-1]
    unavailable = latest.model_copy(
        update={
            "availability_timestamp": BASE + timedelta(days=1),
            "decision_timestamp": BASE + timedelta(days=1),
        }
    )
    probe_values: list[dict[tuple[str, int], float | None]] = []

    @dataclass
    class Probe:
        strategy_id: str = "probe"
        strategy_version: str = "1"

        def on_event(self, context: StrategyContext) -> tuple[Order, ...]:
            probe_values.append(dict(context.features))
            return ()

    BacktestEngine(config(strategy_id="probe", end_hours=4)).run(
        market_bars, Probe(), features=(unavailable,)
    )
    assert all(("simple_return", 1) not in value for value in probe_values)


def test_execution_conditional_nontriggers_and_limit_spread_rejection() -> None:
    market_bar = bars(["100"], highs=["101"], lows=["99"])[0]
    limit = order(
        market_bar, "not-touched", OrderSide.BUY, "1", order_type=OrderType.LIMIT, limit_price="90"
    )
    stop = order(
        market_bar, "not-triggered", OrderSide.BUY, "1", order_type=OrderType.STOP, stop_price="110"
    )
    for item in (limit, stop):
        item.transition(OrderStatus.SUBMITTED)
        item.transition(OrderStatus.ACCEPTED)
    simulator = ExecutionSimulator()
    assert simulator.process((limit, stop), market_bar, fill_timestamp=BASE).fills == ()

    blocked_buy = order(
        market_bar, "blocked-buy", OrderSide.BUY, "1", order_type=OrderType.LIMIT, limit_price="99"
    )
    blocked_buy.transition(OrderStatus.SUBMITTED)
    blocked_buy.transition(OrderStatus.ACCEPTED)
    blocked = ExecutionSimulator(
        spread_model=QuoteOrFixedSpreadModel(Decimal("2"))
    ).process((blocked_buy,), market_bar, fill_timestamp=BASE)
    assert blocked.fills == ()

    blocked_sell = order(
        market_bar,
        "blocked-sell",
        OrderSide.SELL,
        "1",
        order_type=OrderType.LIMIT,
        limit_price="101",
    )
    blocked_sell.transition(OrderStatus.SUBMITTED)
    blocked_sell.transition(OrderStatus.ACCEPTED)
    blocked = ExecutionSimulator(
        spread_model=QuoteOrFixedSpreadModel(Decimal("2"))
    ).process((blocked_sell,), market_bar, fill_timestamp=BASE)
    assert blocked.fills == ()


def test_commission_and_portfolio_accounting_cover_short_and_partial_paths() -> None:
    market_bar = bars(["100"])[0]
    commission_bar = market_bar.model_copy(
        update={
            "instrument": market_bar.instrument.model_copy(update={"trading_currency": None}),
            "currency": None,
        }
    )
    commission_order = order(commission_bar, "commission", OrderSide.BUY, "1")
    commission_order.transition(OrderStatus.SUBMITTED)
    commission_order.transition(OrderStatus.ACCEPTED)
    with pytest.raises(ExecutionPolicyError):
        ExecutionSimulator().process((commission_order,), commission_bar, fill_timestamp=BASE)

    portfolio = Portfolio(starting_cash=Decimal("5000"), account_currency="USD")
    portfolio.apply_fill(_fill(market_bar, "buy-1", OrderSide.BUY, "10", "100", commission="1"))
    portfolio.apply_fill(_fill(market_bar, "buy-2", OrderSide.BUY, "10", "120", commission="1"))
    snapshot = portfolio.snapshot(BASE)
    assert snapshot.positions[0].average_entry_price == Decimal("110")
    portfolio.apply_fill(_fill(market_bar, "sell-part", OrderSide.SELL, "5", "130", commission="1"))
    assert portfolio.trades[-1].gross_pnl == Decimal("100")
    portfolio.apply_fill(_fill(market_bar, "sell-rest", OrderSide.SELL, "15", "90", commission="1"))
    assert portfolio.equity(BASE) == Decimal("4796")

    short_portfolio = Portfolio(starting_cash=Decimal("5000"), account_currency="USD")
    short_portfolio.apply_fill(_fill(market_bar, "short", OrderSide.SELL, "10", "100"))
    short_portfolio.apply_fill(_fill(market_bar, "cover", OrderSide.BUY, "10", "90"))
    assert short_portfolio.trades[0].gross_pnl == Decimal("100")


def test_portfolio_rejects_missing_currency_duplicate_seed_and_invalid_starting_cash() -> None:
    market_bar = bars(["100"])[0]
    with pytest.raises(AccountingError):
        Portfolio(starting_cash=Decimal("0"), account_currency="USD")
    portfolio = Portfolio(starting_cash=Decimal("1000"), account_currency="USD")
    portfolio.seed_position(market_bar.instrument, Decimal("1"), Decimal("100"), BASE)
    with pytest.raises(AccountingError):
        portfolio.seed_position(market_bar.instrument, Decimal("1"), Decimal("100"), BASE)
    with pytest.raises(AccountingError):
        portfolio.seed_position(market_bar.instrument, Decimal("0"), Decimal("100"), BASE)
    no_currency = market_bar.instrument.model_copy(update={"trading_currency": None})
    with pytest.raises(AccountingError):
        Portfolio(starting_cash=Decimal("1000"), account_currency="USD").seed_position(
            no_currency, Decimal("1"), Decimal("100"), BASE
        )
    with pytest.raises(AccountingError):
        StaticCurrencyConverter({}).convert(Decimal("1"), "EUR", "USD", BASE)


def test_metrics_constant_returns_and_zero_denominator_are_explicit() -> None:
    constant_curve = tuple(
        EquitySnapshot(
            timestamp=BASE + timedelta(days=index),
            cash=Decimal("100"),
            gross_market_value=Decimal("0"),
            net_market_value=Decimal("0"),
            equity=Decimal("100"),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            fees=Decimal("0"),
            gross_exposure=Decimal("0"),
            positions=(),
        )
        for index in range(3)
    )
    metrics = calculate_metrics(
        constant_curve,
        (),
        starting_equity=Decimal("0"),
        annualization_factor=2,
        total_fees=Decimal("0"),
    )
    assert metrics.total_return is None
    assert metrics.sharpe_ratio is None
    assert metrics.sortino_ratio is None
