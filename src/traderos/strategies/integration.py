"""Adapter from signal-only Phase 4 strategies to the Phase 3 backtester."""

from decimal import Decimal

from traderos.backtesting.models import Order
from traderos.backtesting.models import StrategyContext as BacktestContext
from traderos.strategies.base import Strategy
from traderos.strategies.errors import StrategyConfigurationError
from traderos.strategies.models import StrategyContext, StrategyResult


class BacktestStrategyAdapter:
    """Translate strategy results into Phase 3 orders without bypassing execution."""

    def __init__(self, strategy: Strategy) -> None:
        self.strategy = strategy
        self.strategy_id = strategy.strategy_id
        self.strategy_version = strategy.strategy_version
        self._decisions: list[StrategyResult] = []

    def on_event(self, context: BacktestContext) -> tuple[Order, ...]:
        position = next(
            (
                snapshot.quantity
                for snapshot in context.positions
                if snapshot.symbol == context.bar.symbol
            ),
            Decimal("0"),
        )
        strategy_context = StrategyContext(
            event_timestamp=context.event_timestamp,
            bar=context.bar,
            feature_observations=context.feature_observations,
            position_quantity=position,
            cash=context.cash,
            equity=context.equity,
            parameters=self.strategy.parameters,
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
        )
        decision = self.strategy.on_bar(strategy_context)
        signal = decision.signal
        if (
            signal.strategy_id != self.strategy_id
            or signal.strategy_version != self.strategy_version
            or signal.instrument != context.bar.instrument
            or signal.timestamp != context.event_timestamp
            or signal.timeframe is not context.bar.timeframe
        ):
            raise StrategyConfigurationError("strategy signal provenance does not match context")
        self._decisions.append(decision)
        orders = []
        for index, intent in enumerate(decision.order_intents):
            if (
                intent.strategy_id != self.strategy_id
                or intent.strategy_version != self.strategy_version
                or intent.instrument != context.bar.instrument
                or intent.signal_id != signal.signal_id
            ):
                raise StrategyConfigurationError("order intent provenance does not match signal")
            orders.append(
                Order(
                    order_id=f"{intent.signal_id}-{index}",
                    instrument=intent.instrument,
                    side=intent.side,
                    quantity=intent.quantity,
                    order_type=intent.order_type,
                    limit_price=intent.limit_price,
                    stop_price=intent.stop_price,
                    time_in_force=intent.time_in_force,
                    strategy_id=intent.strategy_id,
                    strategy_version=intent.strategy_version,
                    metadata={
                        "signal_id": intent.signal_id,
                        "reason": intent.reason,
                        "direction": decision.signal.direction.value,
                    },
                )
            )
        return tuple(orders)

    @property
    def decisions(self) -> tuple[StrategyResult, ...]:
        return tuple(self._decisions)


__all__ = ["BacktestStrategyAdapter"]
