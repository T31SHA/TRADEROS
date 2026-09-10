"""Chronological event-driven backtest orchestration."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType

from traderos.backtesting.errors import (
    BacktestConfigurationError,
    CausalityViolation,
    InvalidMarketDataError,
    InvalidOrderError,
    OrderTimingError,
)
from traderos.backtesting.execution import (
    CommissionModel,
    ExecutionSimulator,
    FixedSlippageModel,
    QuoteOrFixedSpreadModel,
)
from traderos.backtesting.metrics import calculate_metrics
from traderos.backtesting.models import (
    BacktestConfig,
    BacktestResult,
    EquitySnapshot,
    ExecutionPolicy,
    Fill,
    LedgerEntry,
    MissingBarPolicy,
    Order,
    OrderStatus,
    StrategyContext,
    TradeRecord,
)
from traderos.backtesting.portfolio import CurrencyConverter, Portfolio
from traderos.backtesting.strategies import Strategy
from traderos.data.bars import MarketBar
from traderos.data.calendars import MarketCalendar
from traderos.data.time import require_utc
from traderos.features.models import FeatureObservation, FeatureStatus

FeatureParameterKey = tuple[tuple[str, str], ...]
FeatureMapKey = tuple[str, datetime, tuple[str, int, FeatureParameterKey]]


def _parameter_key(observation: FeatureObservation) -> FeatureParameterKey:
    return tuple(
        sorted((name, str(value)) for name, value in observation.lineage.parameters.items())
    )


@dataclass
class _ScheduledOrder:
    order: Order
    eligible_timestamp: datetime
    submitted_symbol: str
    submission_index: int


class BacktestEngine:
    """Replay canonical bars with conservative next-bar execution."""

    def __init__(
        self,
        config: BacktestConfig,
        *,
        converter: CurrencyConverter | None = None,
        calendar: MarketCalendar | None = None,
    ) -> None:
        if config.execution_policy is not ExecutionPolicy.NEXT_BAR_OPEN:
            raise BacktestConfigurationError("only NEXT_BAR_OPEN is implemented")
        if config.missing_bar_policy is MissingBarPolicy.REJECT and calendar is None:
            raise BacktestConfigurationError(
                "missing-bar rejection requires a market calendar"
            )
        self.config = config
        self.converter = converter
        self.calendar = calendar
        self.execution = ExecutionSimulator(
            spread_model=QuoteOrFixedSpreadModel(config.spread.fallback_absolute),
            slippage_model=FixedSlippageModel(config.slippage.absolute),
            commission_model=CommissionModel(config.commission),
            max_fill_quantity=config.max_fill_quantity,
            ambiguity_policy=config.ambiguity_policy,
        )

    def run(
        self,
        bars: Sequence[MarketBar],
        strategy: Strategy,
        *,
        features: Iterable[FeatureObservation] = (),
    ) -> BacktestResult:
        if strategy.strategy_id != self.config.strategy_id:
            raise BacktestConfigurationError("strategy_id does not match backtest configuration")
        if strategy.strategy_version != self.config.strategy_version:
            raise BacktestConfigurationError(
                "strategy_version does not match backtest configuration"
            )
        ordered = self._validate_and_order_bars(bars)
        feature_map = self._validate_features(features, ordered)
        portfolio = Portfolio(
            starting_cash=self.config.starting_cash,
            account_currency=self.config.account_currency,
            converter=self.converter,
        )
        pending: list[_ScheduledOrder] = []
        all_orders: list[Order] = []
        all_fills: list[Fill] = []
        ledger: list[LedgerEntry] = []
        trades: list[TradeRecord] = []
        equity_curve: list[EquitySnapshot] = []
        rejected_orders: list[str] = []
        seen_order_ids: set[str] = set()
        seeded_symbols: set[str] = set()
        turnover_notional = Decimal("0")

        for index, bar in enumerate(ordered):
            event_timestamp = bar.timestamp + bar.timeframe.duration
            self._seed_initial_position(portfolio, bar, event_timestamp, seeded_symbols)
            eligible, pending = self._eligible_orders(pending, bar, index)
            report = self.execution.process(
                eligible,
                bar,
                fill_timestamp=bar.timestamp,
            )
            rejected_orders.extend(report.rejected_order_ids)
            for fill in report.fills:
                entry, closed_trades = portfolio.apply_fill(fill)
                ledger.append(entry)
                trades.extend(closed_trades)
                all_fills.append(fill)
                turnover_notional += abs(
                    portfolio.convert_to_account(
                        fill.notional,
                        fill.instrument.quote_currency or fill.instrument.trading_currency,
                        fill.timestamp,
                    )
                )
            for order in eligible:
                if order.is_open:
                    pending.append(
                        _ScheduledOrder(
                            order=order,
                            eligible_timestamp=bar.timestamp + bar.timeframe.duration,
                            submitted_symbol=bar.symbol,
                            submission_index=index,
                        )
                    )
            portfolio.mark_to_market(bar, event_timestamp)
            snapshot = portfolio.snapshot(event_timestamp)
            equity_curve.append(snapshot)
            visible_features = self._visible_features(feature_map, bar, event_timestamp)
            visible_observations = self._visible_feature_observations(
                feature_map, bar, event_timestamp
            )
            context = StrategyContext(
                event_timestamp=event_timestamp,
                bar=bar,
                features=MappingProxyType(visible_features),
                cash=snapshot.cash,
                equity=snapshot.equity,
                positions=snapshot.positions,
                feature_observations=visible_observations,
            )
            try:
                proposals = tuple(strategy.on_event(context))
            except Exception as exc:
                raise BacktestConfigurationError(
                    f"strategy failed at {event_timestamp.isoformat()}"
                ) from exc
            for order in proposals:
                all_orders.append(order)
                rejection = self._submit_order(
                    order,
                    strategy,
                    event_timestamp,
                    bar,
                    index,
                    seen_order_ids,
                )
                if rejection is not None:
                    rejected_orders.append(rejection)
                    continue
                pending.append(
                    _ScheduledOrder(
                        order=order,
                        eligible_timestamp=event_timestamp,
                        submitted_symbol=bar.symbol,
                        submission_index=index,
                    )
                )

        for scheduled in pending:
            if scheduled.order.is_open:
                scheduled.order.transition(OrderStatus.EXPIRED, reason="end_of_data")
                rejected_orders.append(f"{scheduled.order.order_id}:end_of_data")

        metrics = calculate_metrics(
            equity_curve,
            trades,
            starting_equity=self.config.starting_cash,
            annualization_factor=self.config.annualization_factor,
            risk_free_rate=self.config.risk_free_rate,
            turnover_notional=float(turnover_notional),
            total_fees=portfolio.fees,
        )
        open_orders = tuple(scheduled.order for scheduled in pending if scheduled.order.is_open)
        return BacktestResult(
            experiment_id=self.config.experiment_id,
            config=self.config,
            event_count=len(ordered),
            decision_count=len(ordered),
            orders=tuple(all_orders),
            fills=tuple(all_fills),
            ledger=tuple(ledger),
            trades=tuple(trades),
            equity_curve=tuple(equity_curve),
            metrics=metrics,
            rejected_orders=tuple(rejected_orders),
            open_orders=open_orders,
        )

    def _validate_and_order_bars(self, bars: Sequence[MarketBar]) -> tuple[MarketBar, ...]:
        if not bars:
            raise InvalidMarketDataError("at least one market bar is required")
        symbols = set(self.config.instrument_symbols)
        candidates: list[MarketBar] = []
        for bar in bars:
            require_utc(bar.timestamp)
            if bar.symbol not in symbols:
                raise InvalidMarketDataError(f"bar symbol {bar.symbol} is outside the universe")
            if bar.timeframe is not self.config.timeframe:
                raise InvalidMarketDataError("all bars must use the configured timeframe")
            if bar.adjustment_policy is not self.config.adjustment_policy:
                raise InvalidMarketDataError("raw and adjusted bars cannot be mixed")
            if not self.config.start <= bar.timestamp < self.config.end:
                raise InvalidMarketDataError("bar timestamp is outside the configured range")
            if self.calendar is not None and not self.calendar.is_open_at(bar.timestamp):
                raise InvalidMarketDataError(
                    f"bar is outside the configured market calendar: {bar.timestamp}"
                )
            candidates.append(bar)
        ordered = tuple(
            sorted(candidates, key=lambda item: (item.timestamp, item.symbol, item.source))
        )
        seen: set[tuple[str, datetime]] = set()
        for bar in ordered:
            key = (bar.symbol, bar.timestamp)
            if key in seen:
                raise InvalidMarketDataError(f"duplicate market event: {key!r}")
            seen.add(key)
        if self.config.missing_bar_policy is MissingBarPolicy.REJECT and self.calendar is not None:
            self._reject_calendar_gaps(ordered)
        return ordered

    def _reject_calendar_gaps(self, bars: Sequence[MarketBar]) -> None:
        if self.calendar is None:
            raise BacktestConfigurationError("calendar is required for gap rejection")
        expected = self.calendar.expected_bar_timestamps(
            self.config.start, self.config.end, self.config.timeframe
        )
        expected_set = set(expected)
        for symbol in self.config.instrument_symbols:
            observed = {bar.timestamp for bar in bars if bar.symbol == symbol}
            missing = expected_set - observed
            if missing:
                raise InvalidMarketDataError(
                    f"missing calendar bars for {symbol}: {sorted(missing)[0].isoformat()}"
                )

    def _validate_features(
        self,
        features: Iterable[FeatureObservation],
        bars: Sequence[MarketBar],
    ) -> dict[FeatureMapKey, FeatureObservation]:
        result: dict[FeatureMapKey, FeatureObservation] = {}
        allowed_symbols = set(self.config.instrument_symbols)
        allowed_versions = set(self.config.feature_versions)
        for observation in features:
            if observation.instrument.canonical_symbol not in allowed_symbols:
                raise CausalityViolation("feature belongs to an instrument outside the universe")
            if observation.timeframe is not self.config.timeframe:
                raise CausalityViolation("feature timeframe differs from backtest timeframe")
            if observation.lineage.source_dataset_version != self.config.dataset_version:
                raise CausalityViolation("feature dataset version differs from backtest dataset")
            if observation.lineage.adjustment_policy is not self.config.adjustment_policy:
                raise CausalityViolation("feature adjustment policy differs from backtest")
            key = (
                observation.instrument.canonical_symbol,
                observation.observation_timestamp,
                (
                    observation.feature_name,
                    observation.feature_version,
                    _parameter_key(observation),
                ),
            )
            if key in result:
                raise CausalityViolation(f"duplicate feature observation: {key!r}")
            if allowed_versions and key[2][:2] not in allowed_versions:
                raise CausalityViolation("feature version was not declared in backtest config")
            result[key] = observation
        supplied_versions = {key[2][:2] for key in result}
        if allowed_versions and not allowed_versions.issubset(supplied_versions):
            raise BacktestConfigurationError("configured feature versions were not supplied")
        return result

    @staticmethod
    def _visible_features(
        feature_map: dict[FeatureMapKey, FeatureObservation],
        bar: MarketBar,
        event_timestamp: datetime,
    ) -> dict[tuple[str, int], float | None]:
        visible: dict[tuple[str, int], float | None] = {}
        for (symbol, observation_timestamp, feature_key), observation in feature_map.items():
            if symbol != bar.symbol:
                continue
            if observation_timestamp > bar.timestamp:
                continue
            if observation.availability_timestamp > event_timestamp:
                continue
            visible[(feature_key[0], feature_key[1])] = (
                observation.value if observation.status is FeatureStatus.VALUE else None
            )
        return visible

    @staticmethod
    def _visible_feature_observations(
        feature_map: dict[FeatureMapKey, FeatureObservation],
        bar: MarketBar,
        event_timestamp: datetime,
    ) -> tuple[FeatureObservation, ...]:
        visible = [
            observation
            for (symbol, observation_timestamp, _), observation in feature_map.items()
            if symbol == bar.symbol
            and observation_timestamp <= bar.timestamp
            and observation.availability_timestamp <= event_timestamp
        ]
        return tuple(
            sorted(
                visible,
                key=lambda item: (
                    item.observation_timestamp,
                    item.feature_name,
                    item.feature_version,
                    _parameter_key(item),
                ),
            )
        )

    @staticmethod
    def _eligible_orders(
        pending: Sequence[_ScheduledOrder], bar: MarketBar, index: int
    ) -> tuple[list[Order], list[_ScheduledOrder]]:
        eligible: list[Order] = []
        remaining: list[_ScheduledOrder] = []
        for scheduled in pending:
            order = scheduled.order
            if not order.is_open:
                continue
            if (
                order.time_in_force.value == "day"
                and order.submitted_timestamp is not None
                and bar.timestamp.date() > order.submitted_timestamp.date()
            ):
                order.transition(OrderStatus.EXPIRED, reason="day_order_expired")
                continue
            if scheduled.submission_index >= index:
                remaining.append(scheduled)
                continue
            if bar.symbol != order.instrument.canonical_symbol:
                remaining.append(scheduled)
                continue
            if bar.timestamp < scheduled.eligible_timestamp:
                remaining.append(scheduled)
                continue
            if (
                bar.timestamp == scheduled.eligible_timestamp
                and scheduled.submitted_symbol != bar.symbol
            ):
                remaining.append(scheduled)
                continue
            eligible.append(order)
        return eligible, remaining

    def _seed_initial_position(
        self,
        portfolio: Portfolio,
        bar: MarketBar,
        event_timestamp: datetime,
        seeded_symbols: set[str],
    ) -> None:
        if bar.symbol in seeded_symbols:
            return
        initial = next(
            (
                position
                for position in self.config.initial_positions
                if position.symbol == bar.symbol
            ),
            None,
        )
        if initial is not None:
            portfolio.seed_position(bar.instrument, initial.quantity, bar.open, bar.timestamp)
        seeded_symbols.add(bar.symbol)

    @staticmethod
    def _submit_order(
        order: Order,
        strategy: Strategy,
        event_timestamp: datetime,
        bar: MarketBar,
        index: int,
        seen_order_ids: set[str],
    ) -> str | None:
        try:
            if order.order_id in seen_order_ids:
                raise InvalidOrderError("duplicate order_id")
            seen_order_ids.add(order.order_id)
            if order.instrument.canonical_symbol != bar.symbol:
                raise InvalidOrderError("orders must target the current instrument")
            if order.status is not OrderStatus.CREATED:
                raise InvalidOrderError("strategy orders must begin in CREATED state")
            if order.created_timestamp is not None and order.created_timestamp != event_timestamp:
                raise OrderTimingError("created_timestamp differs from decision timestamp")
            if (
                order.submitted_timestamp is not None
                and order.submitted_timestamp != event_timestamp
            ):
                raise OrderTimingError("submitted_timestamp differs from decision timestamp")
            order.created_timestamp = event_timestamp
            order.submitted_timestamp = event_timestamp
            order.strategy_id = order.strategy_id or strategy.strategy_id
            order.strategy_version = order.strategy_version or strategy.strategy_version
            if (
                order.strategy_id != strategy.strategy_id
                or order.strategy_version != strategy.strategy_version
            ):
                raise InvalidOrderError("order strategy metadata does not match strategy")
            order.transition(OrderStatus.SUBMITTED)
            order.transition(OrderStatus.ACCEPTED)
            del index
            return None
        except (InvalidOrderError, OrderTimingError, ValueError) as exc:
            if order.status is OrderStatus.CREATED:
                order.transition(OrderStatus.REJECTED, reason=str(exc))
            return f"{order.order_id}:{exc}"


__all__ = ["BacktestEngine"]
