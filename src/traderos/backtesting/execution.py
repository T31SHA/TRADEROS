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


@dataclass(frozen=True)
class QuoteOrFixedSpreadModel:
    """Use bar bid/ask when present, otherwise a configured absolute spread."""

    fallback_absolute: Decimal = Decimal("0")

    def executable_price(self, bar: MarketBar, side: OrderSide, reference: Decimal) -> Decimal:
        if bar.bid is not None and bar.ask is not None:
            return bar.ask if side is OrderSide.BUY else bar.bid
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
        if self.absolute < 0 or not self.absolute.is_finite():
            raise ExecutionPolicyError("slippage must be finite and non-negative")
        return reference + self.absolute if side is OrderSide.BUY else reference - self.absolute


@dataclass(frozen=True)
class CommissionModel:
    """Per-unit plus notional-rate commission with a minimum charge."""

    config: CostConfig

    def calculate(self, quantity: Decimal, price: Decimal, bar: MarketBar) -> Commission:
        amount = self.config.per_unit * quantity + self.config.rate * quantity * price
        amount = max(amount, self.config.minimum)
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
            candidate = self._candidate(order, bar)
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
                timestamp=fill_timestamp,
                strategy_id=order.strategy_id,
                strategy_version=order.strategy_version,
            )
            order.record_fill(quantity)
            fills.append(fill)
        return ExecutionReport(tuple(fills), tuple(rejected))

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
