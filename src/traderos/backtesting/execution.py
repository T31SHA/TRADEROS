"""Deterministic next-bar execution, spread, slippage, and commission models."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from traderos.backtesting.errors import ExecutionPolicyError
from traderos.backtesting.models import (
    Commission,
    CostConfig,
    Fill,
    IntrabarAmbiguityPolicy,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from traderos.data.bars import MarketBar


class SpreadModel(Protocol):
    """Return the executable side of a bar price."""

    def executable_price(self, bar: MarketBar, side: OrderSide, reference: Decimal) -> Decimal:
        """Return ask-like buy or bid-like sell price."""


class SlippageModel(Protocol):
    """Apply deterministic adverse slippage."""

    def apply(self, side: OrderSide, reference: Decimal) -> Decimal:
        """Return the slipped price."""


class TransactionCostModel(Protocol):
    """Calculate commission once for one fill slice."""

    def calculate(self, quantity: Decimal, price: Decimal, bar: MarketBar) -> Commission:
        """Return commission and its settlement currency."""


def adverse_execution_price(side: OrderSide, reference: Decimal, absolute: Decimal) -> Decimal:
    """Apply the canonical fixed adverse-slippage rule.

    This deliberately small primitive is shared by historical and paper
    execution.  Adapters own their market-event shape (a bar versus a quote),
    but neither adapter gets to redefine the financial cost rule.
    """

    if not reference.is_finite() or reference <= 0:
        raise ExecutionPolicyError("execution reference must be finite and positive")
    if not absolute.is_finite() or absolute < 0:
        raise ExecutionPolicyError("slippage must be finite and non-negative")
    price = reference + absolute if side is OrderSide.BUY else reference - absolute
    if not price.is_finite() or price <= 0:
        raise ExecutionPolicyError("execution price must be finite and positive")
    return price


def commission_amount(quantity: Decimal, price: Decimal, config: CostConfig) -> Decimal:
    """Calculate one canonical Decimal commission amount for a fill slice."""

    if not quantity.is_finite() or quantity <= 0:
        raise ExecutionPolicyError("commission quantity must be finite and positive")
    if not price.is_finite() or price <= 0:
        raise ExecutionPolicyError("commission price must be finite and positive")
    return max(
        config.per_unit * quantity + config.rate * quantity * price,
        config.minimum,
    )


@dataclass(frozen=True)
class QuoteOrFixedSpreadModel:
    """Use only quotes available at the simulated event, else fixed spread.

    Observed quotes may be transformed for a stress scenario, but source bars
    are never modified.  The simulator supplies a quote-safe bar before this
    model is called.
    """

    fallback_absolute: Decimal = Decimal("0")
    observed_multiplier: Decimal = Decimal("1")

    def executable_price(self, bar: MarketBar, side: OrderSide, reference: Decimal) -> Decimal:
        if bar.bid is not None and bar.ask is not None:
            midpoint = (bar.bid + bar.ask) / Decimal("2")
            half_spread = (bar.ask - bar.bid) / Decimal("2") * self.observed_multiplier
            return midpoint + half_spread if side is OrderSide.BUY else midpoint - half_spread
        half = self.fallback_absolute / Decimal("2")
        return reference + half if side is OrderSide.BUY else reference - half


@dataclass(frozen=True)
class ZeroSlippageModel:
    """Debugging-only zero slippage model."""

    def apply(self, side: OrderSide, reference: Decimal) -> Decimal:
        del side
        return reference


@dataclass(frozen=True)
class FixedSlippageModel:
    """Fixed adverse absolute slippage per unit."""

    absolute: Decimal = Decimal("0")

    def apply(self, side: OrderSide, reference: Decimal) -> Decimal:
        return adverse_execution_price(side, reference, self.absolute)


@dataclass(frozen=True)
class CommissionModel:
    """Per-unit plus notional-rate commission with a minimum charge."""

    config: CostConfig

    def calculate(self, quantity: Decimal, price: Decimal, bar: MarketBar) -> Commission:
        amount = commission_amount(quantity, price, self.config)
        currency = bar.instrument.trading_currency or bar.currency or bar.instrument.quote_currency
        if currency is None:
            raise ExecutionPolicyError("commission currency is unavailable for instrument")
        return Commission(amount=amount, currency=currency)


@dataclass(frozen=True)
class ExecutionReport:
    """Fills and explicit rejections produced for one market bar."""

    fills: tuple[Fill, ...]
    rejected_order_ids: tuple[str, ...]


class ExecutionSimulator:
    """Process only orders explicitly made eligible by the event loop."""

    def __init__(
        self,
        *,
        spread_model: SpreadModel | None = None,
        slippage_model: SlippageModel | None = None,
        commission_model: TransactionCostModel | None = None,
        max_fill_quantity: Decimal | None = None,
        ambiguity_policy: IntrabarAmbiguityPolicy = IntrabarAmbiguityPolicy.REJECT,
    ) -> None:
        self.spread_model = spread_model or QuoteOrFixedSpreadModel()
        self.slippage_model = slippage_model or ZeroSlippageModel()
        self.commission_model = commission_model or CommissionModel(CostConfig())
        if max_fill_quantity is not None and (
            not max_fill_quantity.is_finite() or max_fill_quantity <= 0
        ):
            raise ExecutionPolicyError("max_fill_quantity must be finite and positive")
        self.max_fill_quantity = max_fill_quantity
        self.ambiguity_policy = ambiguity_policy

    def process(
        self,
        orders: Iterable[Order],
        bar: MarketBar,
        *,
        fill_timestamp: datetime,
    ) -> ExecutionReport:
        """Evaluate eligible orders against one completed market bar.

        The engine passes the next bar's opening timestamp as ``fill_timestamp``.
        Conditional orders use OHLC only to determine whether the level was
        touched; ambiguous multiple conditional triggers are rejected by default.
        """

        candidates: list[tuple[Order, Decimal, Decimal, Decimal]] = []
        conditional: list[Order] = []
        rejected: list[str] = []
        for order in orders:
            if order.instrument != bar.instrument or not order.is_open:
                continue
            # A conditional order is evaluated only after the completed bar
            # is observable.  Its fill timestamp is therefore the bar end;
            # market orders remain bar-open executions.
            order_fill_timestamp = (
                fill_timestamp
                if order.order_type is OrderType.MARKET
                else bar.timestamp + bar.timeframe.duration
            )
            quote_safe_bar = self._quote_safe_bar(bar, order_fill_timestamp)
            candidate = self._candidate(order, quote_safe_bar)
            if candidate is None:
                if order.time_in_force.value == "ioc":
                    order.transition(OrderStatus.EXPIRED, reason="ioc_not_filled")
                    rejected.append(order.order_id)
                continue
            reference, execution_price, slippage = candidate
            if order.order_type is not OrderType.MARKET:
                conditional.append(order)
            candidates.append((order, reference, execution_price, slippage))

        if len(conditional) > 1:
            if self.ambiguity_policy is IntrabarAmbiguityPolicy.REJECT:
                for order in conditional:
                    order.transition(OrderStatus.REJECTED, reason="ambiguous_intrabar_path")
                    rejected.append(order.order_id)
                candidates = [item for item in candidates if item[0] not in conditional]
            else:
                chosen = sorted(conditional, key=lambda item: item.order_id)[0]
                for order in conditional:
                    if order is not chosen:
                        order.transition(OrderStatus.REJECTED, reason="ambiguous_intrabar_path")
                        rejected.append(order.order_id)
                candidates = [
                    item for item in candidates if item[0] not in conditional or item[0] is chosen
                ]

        fills: list[Fill] = []
        for order, reference, execution_price, slippage in candidates:
            quantity = order.remaining_quantity
            if self.max_fill_quantity is not None:
                quantity = min(quantity, self.max_fill_quantity)
            if quantity <= 0:
                continue
            commission = self.commission_model.calculate(quantity, execution_price, bar)
            fill = Fill(
                fill_id=f"{order.order_id}-{order.filled_quantity + quantity}",
                order_id=order.order_id,
                instrument=order.instrument,
                side=order.side,
                quantity=quantity,
                price=execution_price,
                reference_price=reference,
                slippage=slippage,
                commission=commission.amount,
                commission_currency=commission.currency,
                timestamp=(
                    fill_timestamp
                    if order.order_type is OrderType.MARKET
                    else bar.timestamp + bar.timeframe.duration
                ),
                strategy_id=order.strategy_id,
                strategy_version=order.strategy_version,
            )
            order.record_fill(quantity)
            if order.time_in_force.value == "ioc" and order.is_open:
                order.transition(OrderStatus.EXPIRED, reason="ioc_remainder_cancelled")
            fills.append(fill)
        return ExecutionReport(tuple(fills), tuple(rejected))

    @staticmethod
    def _quote_safe_bar(bar: MarketBar, fill_timestamp: datetime) -> MarketBar:
        """Hide observations that were not available at this fill instant."""

        if (
            bar.quote_timestamp is not None
            and bar.bid is not None
            and bar.ask is not None
            and bar.quote_timestamp <= fill_timestamp
        ):
            return bar
        return bar.model_copy(update={"bid": None, "ask": None, "spread": None})

    def _candidate(self, order: Order, bar: MarketBar) -> tuple[Decimal, Decimal, Decimal] | None:
        if order.order_type is OrderType.MARKET:
            reference = bar.open
            quoted = self.spread_model.executable_price(bar, order.side, reference)
            slipped = self.slippage_model.apply(order.side, quoted)
            self._require_positive(slipped)
            return reference, slipped, self._adverse_distance(order.side, quoted, slipped)

        if order.order_type is OrderType.LIMIT:
            assert order.limit_price is not None
            touched = (
                bar.low <= order.limit_price
                if order.side is OrderSide.BUY
                else bar.high >= order.limit_price
            )
            if not touched:
                return None
            reference = (
                min(bar.open, order.limit_price)
                if order.side is OrderSide.BUY
                else max(bar.open, order.limit_price)
            )
            quoted = self.spread_model.executable_price(bar, order.side, reference)
            # A limit order cannot receive a worse price than its limit. The
            # spread proxy is therefore checked before accepting the fill.
            if order.side is OrderSide.BUY and quoted > order.limit_price:
                return None
            if order.side is OrderSide.SELL and quoted < order.limit_price:
                return None
            self._require_positive(quoted)
            return reference, quoted, self._adverse_distance(order.side, reference, quoted)

        assert order.stop_price is not None
        triggered = (
            bar.high >= order.stop_price
            if order.side is OrderSide.BUY
            else bar.low <= order.stop_price
        )
        if not triggered:
            return None
        reference = (
            max(bar.open, order.stop_price)
            if order.side is OrderSide.BUY
            else min(bar.open, order.stop_price)
        )
        quoted = self.spread_model.executable_price(bar, order.side, reference)
        slipped = self.slippage_model.apply(order.side, quoted)
        self._require_positive(slipped)
        return reference, slipped, self._adverse_distance(order.side, quoted, slipped)

    @staticmethod
    def _adverse_distance(side: OrderSide, reference: Decimal, price: Decimal) -> Decimal:
        distance = price - reference if side is OrderSide.BUY else reference - price
        return max(distance, Decimal("0"))

    @staticmethod
    def _require_positive(price: Decimal) -> None:
        if not price.is_finite() or price <= 0:
            raise ExecutionPolicyError("execution price must be finite and positive")


__all__ = [
    "adverse_execution_price",
    "commission_amount",
    "CommissionModel",
    "ExecutionReport",
    "ExecutionSimulator",
    "FixedSlippageModel",
    "QuoteOrFixedSpreadModel",
    "SlippageModel",
    "SpreadModel",
    "TransactionCostModel",
    "ZeroSlippageModel",
]
