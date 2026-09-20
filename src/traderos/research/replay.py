"""Canonical offline Phase 2→5→4→6→7→8-sizing→3 replay adapter.

The adapter implements only Phase 3's strategy protocol.  It never executes a
fill, calculates P&L, or owns portfolio state: :class:`BacktestEngine` remains
the sole execution and accounting implementation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from traderos.backtesting.models import Order, OrderSide, OrderType, StrategyContext
from traderos.data.bars import MarketBar
from traderos.data.calendars import MarketCalendar
from traderos.data.instruments import Instrument
from traderos.features.engine import FeatureEngine
from traderos.features.models import FeatureContext, FeatureRequest, FeatureSet
from traderos.paper import (
    PaperAccount,
    PaperExecutionConfig,
    PaperPosition,
    PaperQuote,
    PaperTradingError,
    quantity_for_authorization,
)
from traderos.regimes import RegimeContext, RegimeDetector, RegimeEngine, RegimeState
from traderos.risk import (
    MarketDataStatus,
    MarketRiskSnapshot,
    PortfolioRiskSnapshot,
    RiskDecision,
    RiskDecisionStatus,
    RiskEvaluationContext,
    RiskFirewall,
    RiskPositionSnapshot,
    SystemHealthSnapshot,
    SystemHealthStatus,
)
from traderos.signals import (
    FusionContext,
    FusionDirection,
    FusionPolicy,
    SignalFusionEngine,
    UnifiedTradeIntent,
)
from traderos.strategies.base import Strategy as Phase4Strategy
from traderos.strategies.models import StrategyContext as Phase4StrategyContext
from traderos.strategies.models import StrategyResult


class ReplayConfigurationError(ValueError):
    """Raised when a historical replay would bypass a canonical boundary."""


@dataclass(frozen=True)
class ReplayAuditEvent:
    """One auditable decision, including no-trade outcomes."""

    timestamp: datetime
    terminal_state: str
    regime: RegimeState | None
    strategy_results: tuple[StrategyResult, ...]
    intent: UnifiedTradeIntent | None
    risk_decision: RiskDecision | None
    order_id: str | None
    detail: str | None = None


class CanonicalHistoricalReplay:
    """Thin adapter that delegates all execution to the Phase 3 event loop."""

    strategy_id = "canonical_historical_replay"
    strategy_version = "1"

    def __init__(
        self,
        *,
        strategies: Sequence[Phase4Strategy],
        regime_detector: RegimeDetector,
        fusion_policy: FusionPolicy,
        risk_firewall: RiskFirewall,
        calendar: MarketCalendar,
        sizing_config: PaperExecutionConfig,
        account_id: str,
        account_currency: str,
        starting_cash: Decimal,
        instruments: Mapping[str, Instrument] | None = None,
    ) -> None:
        if not strategies:
            raise ReplayConfigurationError(
                "canonical replay requires at least one Phase 4 strategy"
            )
        if not account_id.strip() or not account_currency.strip() or starting_cash <= 0:
            raise ReplayConfigurationError("canonical replay account configuration is invalid")
        keys = [(strategy.strategy_id, strategy.strategy_version) for strategy in strategies]
        if len(set(keys)) != len(keys):
            raise ReplayConfigurationError(
                "canonical replay strategies must have unique identities"
            )
        self._strategies = tuple(strategies)
        self._regimes = RegimeEngine(regime_detector)
        self._fusion = SignalFusionEngine(fusion_policy)
        self._firewall = risk_firewall
        self._calendar = calendar
        self._sizing_config = sizing_config
        self._account_id = account_id
        self._account_currency = account_currency
        self._starting_cash = starting_cash
        self._instruments = dict(instruments or {})
        self._high_water_mark = starting_cash
        self._risk_day: date | None = None
        self._risk_day_starting_equity = starting_cash
        self._audit: list[ReplayAuditEvent] = []

    @staticmethod
    def compute_features(
        *,
        bars: Sequence[MarketBar],
        requests: Iterable[FeatureRequest],
        context: FeatureContext,
    ) -> FeatureSet:
        """Use the existing Phase 2 feature engine; no research indicators exist."""

        return FeatureEngine().compute_feature_set(bars, requests, context)

    @property
    def audit_events(self) -> tuple[ReplayAuditEvent, ...]:
        return tuple(self._audit)

    def _position_quantity(self, context: StrategyContext) -> Decimal:
        return next(
            (
                position.quantity
                for position in context.positions
                if position.symbol == context.bar.symbol
            ),
            Decimal("0"),
        )

    def _risk_snapshot(self, context: StrategyContext) -> PortfolioRiskSnapshot:
        self._advance_risk_day(context)
        self._high_water_mark = max(self._high_water_mark, context.equity)
        positions: list[RiskPositionSnapshot] = []
        for position in context.positions:
            if position.quantity == 0:
                continue
            instrument = self._instruments.get(position.symbol)
            if instrument is None:
                raise ReplayConfigurationError(
                    f"missing explicit instrument identity for position {position.symbol}"
                )
            positions.append(
                RiskPositionSnapshot(instrument=instrument, net_notional=position.market_value)
            )
        return PortfolioRiskSnapshot(
            timestamp=context.event_timestamp,
            account_id=self._account_id,
            account_currency=self._account_currency,
            risk_day=context.event_timestamp.date(),
            equity=context.equity,
            cash=context.cash,
            margin_used=Decimal("0"),
            margin_available=context.equity,
            daily_pnl=context.equity - self._risk_day_starting_equity,
            high_water_mark=self._high_water_mark,
            positions=tuple(positions),
            position_mark_timestamp=context.event_timestamp,
        )

    def _paper_account(
        self, context: StrategyContext, snapshot: PortfolioRiskSnapshot
    ) -> PaperAccount:
        parameters = self._firewall.parameters
        return PaperAccount(
            account_id=self._account_id,
            account_currency=self._account_currency,
            starting_cash=self._starting_cash,
            cash=context.cash,
            reserved_risk=Decimal("0"),
            risk_capacity=parameters.max_gross_exposure,
            daily_loss_limit=parameters.daily_loss_limit,
            max_drawdown=parameters.max_drawdown,
            risk_policy_id=self._firewall.policy_id,
            risk_policy_configuration_id=self._firewall.configuration_id,
            risk_day=snapshot.risk_day,
            risk_day_starting_equity=self._risk_day_starting_equity,
            high_water_mark=snapshot.high_water_mark or self._starting_cash,
            realized_pnl=next(
                (
                    position.realized_pnl
                    for position in context.positions
                    if position.symbol == context.bar.symbol
                ),
                Decimal("0"),
            ),
            fees=sum((position.fees for position in context.positions), Decimal("0")),
            created_at=context.event_timestamp,
            updated_at=context.event_timestamp,
        )

    def _paper_position(self, context: StrategyContext) -> PaperPosition | None:
        position = next(
            (item for item in context.positions if item.symbol == context.bar.symbol), None
        )
        if position is None:
            return None
        return PaperPosition(
            account_id=self._account_id,
            instrument=context.bar.instrument,
            quantity=position.quantity,
            average_entry_price=position.average_entry_price,
            market_price=position.market_price,
            realized_pnl=position.realized_pnl,
            unrealized_pnl=position.unrealized_pnl,
            fees=position.fees,
            updated_at=context.event_timestamp,
        )

    def on_event(self, context: StrategyContext) -> tuple[Order, ...]:
        """Perform canonical decisions at Phase 3's existing completed-bar time."""

        self._advance_risk_day(context)

        regime = self._regimes.evaluate(
            RegimeContext(
                bar=context.bar,
                decision_timestamp=context.event_timestamp,
                feature_observations=context.feature_observations,
                calendar=self._calendar,
                detector_parameters=self._regimes.detector.parameters,
            )
        )
        results = tuple(
            strategy.on_bar(
                Phase4StrategyContext(
                    event_timestamp=context.event_timestamp,
                    bar=context.bar,
                    feature_observations=context.feature_observations,
                    position_quantity=self._position_quantity(context),
                    cash=context.cash,
                    equity=context.equity,
                    parameters=strategy.parameters,
                    strategy_id=strategy.strategy_id,
                    strategy_version=strategy.strategy_version,
                )
            )
            for strategy in self._strategies
        )
        intent = self._fusion.fuse(
            FusionContext(
                context.bar.instrument,
                context.bar.timeframe,
                context.event_timestamp,
                tuple(result.signal for result in results),
                regime,
            )
        )
        if intent.direction not in {FusionDirection.LONG, FusionDirection.SHORT}:
            self._audit.append(
                ReplayAuditEvent(
                    context.event_timestamp,
                    "FUSION_REJECTED",
                    regime,
                    results,
                    intent,
                    None,
                    None,
                    intent.status.value,
                )
            )
            return ()
        snapshot = self._risk_snapshot(context)
        market = MarketRiskSnapshot(
            context.bar.instrument,
            context.event_timestamp,
            MarketDataStatus.VALID
            if context.bar.bid and context.bar.ask
            else MarketDataStatus.UNAVAILABLE,
            context.bar.bid,
            context.bar.ask,
        )
        decision = self._firewall.evaluate(
            RiskEvaluationContext(
                decision_timestamp=context.event_timestamp,
                intent=intent,
                regime_state=regime,
                market=market,
                portfolio=snapshot,
                system_health=SystemHealthSnapshot(
                    context.event_timestamp, SystemHealthStatus.HEALTHY
                ),
            )
        )
        if decision.status is not RiskDecisionStatus.APPROVE:
            self._audit.append(
                ReplayAuditEvent(
                    context.event_timestamp,
                    "RISK_REJECTED",
                    regime,
                    results,
                    intent,
                    decision,
                    None,
                )
            )
            return ()
        assert context.bar.bid is not None and context.bar.ask is not None
        try:
            sized = quantity_for_authorization(
                decision=decision,
                quote=PaperQuote(
                    context.bar.instrument,
                    context.event_timestamp,
                    context.bar.bid,
                    context.bar.ask,
                ),
                account=self._paper_account(context, snapshot),
                position=self._paper_position(context),
                reserved_risk=self._pending_reserved(context, "reserved_risk"),
                reserved_cash=self._pending_reserved(context, "reserved_cash"),
                config=self._sizing_config,
                current_gross_exposure=sum(
                    (abs(position.market_value) for position in context.positions), Decimal("0")
                ),
            )
        except PaperTradingError as exc:
            self._audit.append(
                ReplayAuditEvent(
                    context.event_timestamp,
                    "SIZING_REJECTED",
                    regime,
                    results,
                    intent,
                    decision,
                    None,
                    str(exc),
                )
            )
            return ()
        order = Order(
            order_id=f"{decision.decision_id}:0",
            instrument=context.bar.instrument,
            side=OrderSide.BUY if intent.direction is FusionDirection.LONG else OrderSide.SELL,
            quantity=sized.quantity,
            order_type=OrderType.MARKET,
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            metadata={
                "intent_id": intent.intent_id,
                "risk_decision_id": decision.decision_id,
                "sizing_configuration_id": sized.configuration_id,
                "reserved_risk": str(sized.reserved_risk),
                "reserved_cash": str(sized.reserved_cash),
            },
        )
        self._audit.append(
            ReplayAuditEvent(
                context.event_timestamp,
                "ORDER_CREATED",
                regime,
                results,
                intent,
                decision,
                order.order_id,
            )
        )
        return (order,)

    @staticmethod
    def _pending_reserved(context: StrategyContext, name: str) -> Decimal:
        return sum(
            (Decimal(order.metadata.get(name, "0")) for order in context.pending_orders),
            Decimal("0"),
        )

    def _advance_risk_day(self, context: StrategyContext) -> None:
        current_day = context.event_timestamp.date()
        if self._risk_day is None:
            self._risk_day = current_day
            self._risk_day_starting_equity = context.equity
        elif current_day < self._risk_day:
            raise ReplayConfigurationError("historical replay events moved backward in UTC time")
        elif current_day != self._risk_day:
            self._risk_day = current_day
            self._risk_day_starting_equity = context.equity


__all__ = ["CanonicalHistoricalReplay", "ReplayAuditEvent", "ReplayConfigurationError"]
