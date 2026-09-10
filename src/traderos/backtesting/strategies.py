"""Minimal signal-only strategy contracts and deterministic baselines."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from traderos.backtesting.models import Order, OrderSide, OrderType, StrategyContext


class Strategy(Protocol):
    """Strategy boundary used by the event loop."""

    strategy_id: str
    strategy_version: str

    def on_event(self, context: StrategyContext) -> Iterable[Order]:
        """Return order proposals using only the supplied current context."""


@dataclass
class AlwaysFlatStrategy:
    """No-trade baseline for accounting and data-integrity tests."""

    strategy_id: str = "always_flat"
    strategy_version: str = "1"

    def on_event(self, context: StrategyContext) -> tuple[Order, ...]:
        del context
        return ()


@dataclass
class BuyAndHoldStrategy:
    """Simple deterministic baseline that buys each symbol once."""

    quantities: Mapping[str, Decimal]
    strategy_id: str = "buy_and_hold"
    strategy_version: str = "1"
    _submitted: set[str] | None = None

    def __post_init__(self) -> None:
        self._submitted = set()

    def on_event(self, context: StrategyContext) -> tuple[Order, ...]:
        assert self._submitted is not None
        symbol = context.bar.symbol
        quantity = self.quantities.get(symbol)
        if quantity is None or symbol in self._submitted:
            return ()
        self._submitted.add(symbol)
        return (
            Order(
                order_id=f"{self.strategy_id}-{symbol}-{len(self._submitted)}",
                instrument=context.bar.instrument,
                side=OrderSide.BUY,
                quantity=quantity,
                order_type=OrderType.MARKET,
                strategy_id=self.strategy_id,
                strategy_version=self.strategy_version,
            ),
        )


@dataclass
class FixedOrderStrategy:
    """Deterministic test strategy emitting predeclared orders by timestamp."""

    orders_by_timestamp: Mapping[datetime, tuple[Order, ...]]
    strategy_id: str = "fixed_orders"
    strategy_version: str = "1"

    def on_event(self, context: StrategyContext) -> tuple[Order, ...]:
        orders = self.orders_by_timestamp.get(context.event_timestamp, ())
        return tuple(orders)


__all__ = ["AlwaysFlatStrategy", "BuyAndHoldStrategy", "FixedOrderStrategy", "Strategy"]
