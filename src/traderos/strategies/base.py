"""Strategy protocol and shared decision helpers."""

from collections.abc import Mapping
from decimal import Decimal
from typing import Protocol

from traderos.backtesting.models import OrderSide
from traderos.strategies.errors import StrategyConfigurationError
from traderos.strategies.models import (
    FeatureRequirement,
    OrderIntent,
    SignalDirection,
    StrategyContext,
    StrategyMetadata,
    StrategyResult,
    StrategySignal,
    deterministic_signal_id,
)


class Strategy(Protocol):
    """Signal-only strategy contract consumed by the Phase 4 adapter."""

    strategy_id: str
    strategy_version: str
    parameters: Mapping[str, int | float | str | Decimal]

    @property
    def metadata(self) -> StrategyMetadata:
        """Return immutable identity, scope, and dependency metadata."""

    def on_bar(self, context: StrategyContext) -> StrategyResult:
        """Return a decision using only the supplied completed-bar context."""


class RuleStrategy:
    """Shared validation and target-position conversion for baseline rules."""

    strategy_id: str
    strategy_version: str = "1"

    @property
    def metadata(self) -> StrategyMetadata:
        raise NotImplementedError

    @property
    def parameters(self) -> Mapping[str, int | float | str | Decimal]:
        raise NotImplementedError

    def _validate_context(self, context: StrategyContext) -> None:
        metadata = self.metadata
        if (
            context.strategy_id != self.strategy_id
            or context.strategy_version != self.strategy_version
        ):
            raise StrategyConfigurationError("context strategy metadata does not match strategy")
        if context.asset_class not in metadata.supported_asset_classes:
            raise StrategyConfigurationError(
                f"{self.strategy_id} does not support {context.asset_class.value}"
            )
        if context.timeframe not in metadata.supported_timeframes:
            raise StrategyConfigurationError(
                f"{self.strategy_id} does not support {context.timeframe.value}"
            )

    def _result(
        self,
        context: StrategyContext,
        direction: SignalDirection,
        reason: str,
        requirements: tuple[FeatureRequirement, ...],
        *,
        score: float | None = None,
        target_quantity: Decimal = Decimal("1"),
    ) -> StrategyResult:
        signal = StrategySignal(
            signal_id=deterministic_signal_id(
                self.strategy_id,
                self.strategy_version,
                context.instrument,
                context.timeframe,
                context.event_timestamp,
                direction,
            ),
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            instrument=context.instrument,
            timestamp=context.event_timestamp,
            timeframe=context.timeframe,
            direction=direction,
            reason=reason,
            feature_provenance=requirements,
            score=score,
        )
        if direction in {SignalDirection.HOLD}:
            return StrategyResult(signal)
        target = {
            SignalDirection.LONG: target_quantity,
            SignalDirection.SHORT: -target_quantity,
            SignalDirection.FLAT: Decimal("0"),
        }[direction]
        delta = target - context.position_quantity
        if delta == 0:
            return StrategyResult(signal)
        intent = OrderIntent(
            instrument=context.instrument,
            side=OrderSide.BUY if delta > 0 else OrderSide.SELL,
            quantity=abs(delta),
            signal_id=signal.signal_id,
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            reason=reason,
            target_position=target,
        )
        return StrategyResult(signal, (intent,))


__all__ = ["RuleStrategy", "Strategy"]
