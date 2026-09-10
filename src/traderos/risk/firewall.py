"""Pure, deterministic, fail-closed Phase 7 risk firewall."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from traderos.data.instruments import AssetClass
from traderos.regimes.models import (
    DataSessionState,
    LiquidityRegime,
    TrendRegime,
    VolatilityRegime,
)
from traderos.risk.models import (
    MarketDataStatus,
    PortfolioRiskSnapshot,
    RiskAction,
    RiskAuthorization,
    RiskCheckResult,
    RiskDecision,
    RiskDecisionStatus,
    RiskEvaluationContext,
    RiskLock,
    RiskReasonCode,
    SystemHealthStatus,
    deterministic_risk_decision_id,
)
from traderos.risk.policy import RiskFirewallParameters, risk_configuration_identity
from traderos.signals.models import FusionDirection, FusionStatus

_ZERO = Decimal("0")


def _finite(value: Decimal | None, *, positive: bool = False, nonnegative: bool = False) -> bool:
    return (
        value is not None
        and value.is_finite()
        and (not positive or value > 0)
        and (not nonnegative or value >= 0)
    )


def _decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


@dataclass(frozen=True)
class _Exposure:
    """Derived account-currency exposure from one validated snapshot."""

    gross: Decimal
    net: Decimal
    long: Decimal
    short: Decimal
    instrument: Decimal
    asset_class: Decimal
    open_positions: int


class RiskFirewall:
    """Evaluate immutable state without sizing, execution, or hidden mutation."""

    policy_id = "risk_firewall"
    policy_version = "1"

    def __init__(self, parameters: RiskFirewallParameters | None = None) -> None:
        self.parameters = parameters or RiskFirewallParameters()
        self.configuration_id = risk_configuration_identity(self.parameters)

    @staticmethod
    def _check(
        checks: list[RiskCheckResult],
        check_id: str,
        passed: bool,
        reason: RiskReasonCode | None = None,
        observed: str | None = None,
        limit: str | None = None,
    ) -> bool:
        checks.append(RiskCheckResult(check_id, passed, reason, observed, limit))
        return passed

    def _temporal_check(
        self,
        checks: list[RiskCheckResult],
        *,
        check_id: str,
        timestamp: datetime | None,
        decision_timestamp: datetime,
        maximum_age: timedelta,
        stale_reason: RiskReasonCode,
    ) -> bool:
        if timestamp is None:
            return self._check(
                checks, check_id, False, stale_reason, observed=None, limit=str(maximum_age)
            )
        if timestamp > decision_timestamp:
            return self._check(
                checks,
                check_id,
                False,
                RiskReasonCode.FUTURE_DATED_INPUT,
                timestamp.isoformat(),
                decision_timestamp.isoformat(),
            )
        age = decision_timestamp - timestamp
        return self._check(
            checks,
            check_id,
            age <= maximum_age,
            None if age <= maximum_age else stale_reason,
            str(age),
            str(maximum_age),
        )

    @staticmethod
    def _portfolio_numbers_valid(snapshot: PortfolioRiskSnapshot) -> bool:
        values = (
            snapshot.equity,
            snapshot.cash,
            snapshot.margin_used,
            snapshot.margin_available,
            snapshot.daily_pnl,
            snapshot.high_water_mark,
            *(item.net_notional for item in snapshot.positions),
        )
        return (
            all(_finite(value) for value in values)
            and _finite(snapshot.equity, positive=True)
            and _finite(snapshot.high_water_mark, positive=True)
            and _finite(snapshot.margin_used, nonnegative=True)
            and _finite(snapshot.margin_available, nonnegative=True)
        )

    @staticmethod
    def _exposure(
        snapshot: PortfolioRiskSnapshot, symbol: str, asset_class: AssetClass
    ) -> _Exposure:
        values = tuple(item.net_notional for item in snapshot.positions)
        gross = sum((abs(value) for value in values), _ZERO)
        net = sum(values, _ZERO)
        long = sum((value for value in values if value > 0), _ZERO)
        short = sum((-value for value in values if value < 0), _ZERO)
        instrument = next(
            (
                item.net_notional
                for item in snapshot.positions
                if item.instrument.canonical_symbol == symbol
            ),
            _ZERO,
        )
        asset = sum(
            (
                abs(item.net_notional)
                for item in snapshot.positions
                if item.instrument.asset_class is asset_class
            ),
            _ZERO,
        )
        return _Exposure(gross, net, long, short, instrument, asset, len(snapshot.positions))

    @staticmethod
    def _action(direction: FusionDirection, current_instrument_notional: Decimal) -> RiskAction:
        sign = Decimal("1") if direction is FusionDirection.LONG else Decimal("-1")
        return (
            RiskAction.REDUCTION_ONLY
            if current_instrument_notional * sign < 0
            else RiskAction.NEW_OR_INCREASE
        )

    def _regime_valid(self, context: RiskEvaluationContext, checks: list[RiskCheckResult]) -> bool:
        regime = context.regime_state
        intent = context.intent
        if regime is None or intent.regime_state_id != (regime.state_id if regime else None):
            return self._check(
                checks, "regime", False, RiskReasonCode.REGIME_UNAVAILABLE, observed=None
            )
        if regime.decision_timestamp > context.decision_timestamp:
            return self._check(
                checks,
                "regime",
                False,
                RiskReasonCode.FUTURE_DATED_INPUT,
                regime.decision_timestamp.isoformat(),
                context.decision_timestamp.isoformat(),
            )
        if (
            regime.instrument != intent.instrument
            or regime.timeframe is not intent.timeframe
            or regime.decision_timestamp != context.decision_timestamp
        ):
            return self._check(
                checks, "regime", False, RiskReasonCode.REGIME_UNAVAILABLE, regime.state_id
            )
        if regime.data_session is not DataSessionState.ACTIVE:
            return self._check(
                checks,
                "regime",
                False,
                RiskReasonCode.MARKET_INACTIVE,
                regime.data_session.value,
                DataSessionState.ACTIVE.value,
            )
        if (
            regime.trend is TrendRegime.UNAVAILABLE
            or regime.volatility is VolatilityRegime.UNAVAILABLE
            or regime.liquidity is LiquidityRegime.UNKNOWN
        ):
            return self._check(
                checks, "regime", False, RiskReasonCode.REGIME_UNAVAILABLE, regime.state_id
            )
        if (
            self.parameters.reject_stressed_liquidity
            and regime.liquidity is LiquidityRegime.STRESSED
        ):
            return self._check(
                checks,
                "regime",
                False,
                RiskReasonCode.LIQUIDITY_STRESSED,
                regime.liquidity.value,
            )
        return self._check(checks, "regime", True)

    def _market_valid(self, context: RiskEvaluationContext, checks: list[RiskCheckResult]) -> bool:
        market = context.market
        if market is None:
            return self._check(checks, "market_data", False, RiskReasonCode.DATA_UNAVAILABLE)
        scope_ok = market.instrument == context.intent.instrument
        self._check(
            checks,
            "market_scope",
            scope_ok,
            None if scope_ok else RiskReasonCode.DATA_INVALID,
        )
        self._temporal_check(
            checks,
            check_id="market_freshness",
            timestamp=market.timestamp,
            decision_timestamp=context.decision_timestamp,
            maximum_age=self.parameters.max_market_data_age,
            stale_reason=RiskReasonCode.DATA_STALE,
        )
        status_reason = {
            MarketDataStatus.STALE: RiskReasonCode.DATA_STALE,
            MarketDataStatus.INVALID: RiskReasonCode.DATA_INVALID,
            MarketDataStatus.UNAVAILABLE: RiskReasonCode.DATA_UNAVAILABLE,
        }.get(market.data_status)
        self._check(
            checks,
            "market_data_status",
            market.data_status is MarketDataStatus.VALID,
            status_reason,
            market.data_status.value,
            MarketDataStatus.VALID.value,
        )
        bid_ask_valid = _finite(market.bid, positive=True) and _finite(market.ask, positive=True)
        self._check(
            checks,
            "market_price",
            bid_ask_valid,
            None if bid_ask_valid else RiskReasonCode.PRICE_INVALID,
            f"bid={_decimal(market.bid)},ask={_decimal(market.ask)}",
        )
        if not bid_ask_valid:
            return False
        assert market.bid is not None and market.ask is not None
        non_crossed = market.bid <= market.ask
        self._check(
            checks,
            "market_crossed",
            non_crossed,
            None if non_crossed else RiskReasonCode.CROSSED_MARKET,
            f"bid={market.bid},ask={market.ask}",
        )
        if not non_crossed:
            return False
        midpoint = (market.bid + market.ask) / Decimal("2")
        spread_fraction = (market.ask - market.bid) / midpoint
        self._check(
            checks,
            "spread",
            spread_fraction <= self.parameters.max_spread_fraction,
            None
            if spread_fraction <= self.parameters.max_spread_fraction
            else RiskReasonCode.SPREAD_TOO_WIDE,
            str(spread_fraction),
            str(self.parameters.max_spread_fraction),
        )
        return True

    def _new_risk_limits(
        self, exposure: _Exposure, direction: FusionDirection
    ) -> tuple[tuple[str, Decimal, RiskReasonCode], ...]:
        if direction is FusionDirection.LONG:
            directional_remaining = self.parameters.max_long_exposure - exposure.long
            net_remaining = self.parameters.max_net_exposure - exposure.net
        else:
            directional_remaining = self.parameters.max_short_exposure - exposure.short
            net_remaining = self.parameters.max_net_exposure + exposure.net
        return (
            (
                "gross_exposure",
                self.parameters.max_gross_exposure - exposure.gross,
                RiskReasonCode.MAX_GROSS_EXPOSURE_EXCEEDED,
            ),
            ("net_exposure", net_remaining, RiskReasonCode.MAX_NET_EXPOSURE_EXCEEDED),
            (
                "directional_exposure",
                directional_remaining,
                RiskReasonCode.MAX_LONG_EXPOSURE_EXCEEDED
                if direction is FusionDirection.LONG
                else RiskReasonCode.MAX_SHORT_EXPOSURE_EXCEEDED,
            ),
            (
                "instrument_exposure",
                self.parameters.max_instrument_exposure - abs(exposure.instrument),
                RiskReasonCode.MAX_INSTRUMENT_EXPOSURE_EXCEEDED,
            ),
            (
                "asset_class_exposure",
                self.parameters.max_asset_class_exposure - exposure.asset_class,
                RiskReasonCode.MAX_ASSET_CLASS_EXPOSURE_EXCEEDED,
            ),
        )

    def evaluate(self, context: RiskEvaluationContext) -> RiskDecision:
        """Return a deterministic veto or bounded authorization without side effects."""

        checks: list[RiskCheckResult] = []
        intent = context.intent
        self._check(
            checks,
            "kill_switch",
            not self.parameters.kill_switch_active,
            RiskReasonCode.KILL_SWITCH_ACTIVE if self.parameters.kill_switch_active else None,
        )
        intent_time_ok = intent.decision_timestamp == context.decision_timestamp
        self._check(
            checks,
            "intent_timestamp",
            intent_time_ok,
            None
            if intent_time_ok
            else RiskReasonCode.FUTURE_DATED_INPUT
            if intent.decision_timestamp > context.decision_timestamp
            else RiskReasonCode.INTENT_NOT_ACTIONABLE,
            intent.decision_timestamp.isoformat(),
            context.decision_timestamp.isoformat(),
        )
        actionable = intent.direction in {FusionDirection.LONG, FusionDirection.SHORT} and (
            intent.status is FusionStatus.DIRECTIONAL
        )
        self._check(
            checks,
            "intent",
            actionable,
            None if actionable else RiskReasonCode.INTENT_NOT_ACTIONABLE,
        )
        health = context.system_health
        if health is None:
            self._check(checks, "system_health", False, RiskReasonCode.SYSTEM_UNHEALTHY)
        else:
            self._temporal_check(
                checks,
                check_id="system_health_freshness",
                timestamp=health.timestamp,
                decision_timestamp=context.decision_timestamp,
                maximum_age=self.parameters.max_system_health_age,
                stale_reason=RiskReasonCode.SYSTEM_HEALTH_STALE,
            )
            self._check(
                checks,
                "system_health",
                health.status is SystemHealthStatus.HEALTHY,
                None
                if health.status is SystemHealthStatus.HEALTHY
                else RiskReasonCode.SYSTEM_UNHEALTHY,
                health.status.value,
                SystemHealthStatus.HEALTHY.value,
            )
        self._market_valid(context, checks)
        self._regime_valid(context, checks)

        snapshot = context.portfolio
        exposure: _Exposure | None = None
        action: RiskAction | None = None
        if snapshot is None:
            self._check(checks, "portfolio", False, RiskReasonCode.PORTFOLIO_INVALID)
        else:
            self._temporal_check(
                checks,
                check_id="portfolio_freshness",
                timestamp=snapshot.timestamp,
                decision_timestamp=context.decision_timestamp,
                maximum_age=self.parameters.max_risk_snapshot_age,
                stale_reason=RiskReasonCode.STALE_RISK_SNAPSHOT,
            )
            numbers_valid = self._portfolio_numbers_valid(snapshot)
            self._check(
                checks,
                "portfolio_numeric",
                numbers_valid,
                None if numbers_valid else RiskReasonCode.PORTFOLIO_INVALID,
            )
            if numbers_valid:
                assert snapshot.equity is not None
                assert snapshot.cash is not None
                assert snapshot.margin_available is not None
                assert snapshot.daily_pnl is not None
                assert snapshot.high_water_mark is not None
                exposure = self._exposure(
                    snapshot, intent.instrument.canonical_symbol, intent.asset_class
                )
                action = self._action(intent.direction, exposure.instrument)
                self._check(
                    checks,
                    "duplicate_intent",
                    intent.intent_id not in snapshot.reserved_intent_ids,
                    None
                    if intent.intent_id not in snapshot.reserved_intent_ids
                    else RiskReasonCode.DUPLICATE_INTENT,
                )
                locked = bool(snapshot.active_locks)
                lock_reason = (
                    RiskReasonCode.DRAWDOWN_LOCKED
                    if RiskLock.DRAWDOWN in snapshot.active_locks
                    else RiskReasonCode.RISK_LOCK_ACTIVE
                )
                self._check(
                    checks,
                    "risk_locks",
                    not locked,
                    None if not locked else lock_reason,
                    ",".join(sorted(lock.value for lock in snapshot.active_locks)) or None,
                )
                self._check(
                    checks,
                    "risk_day",
                    snapshot.risk_day == context.decision_timestamp.date(),
                    None
                    if snapshot.risk_day == context.decision_timestamp.date()
                    else RiskReasonCode.RISK_DAY_MISMATCH,
                    snapshot.risk_day.isoformat(),
                    context.decision_timestamp.date().isoformat(),
                )
                self._check(
                    checks,
                    "capital",
                    snapshot.cash >= self.parameters.minimum_cash_buffer,
                    None
                    if snapshot.cash >= self.parameters.minimum_cash_buffer
                    else RiskReasonCode.INSUFFICIENT_CAPITAL,
                    str(snapshot.cash),
                    str(self.parameters.minimum_cash_buffer),
                )
                self._check(
                    checks,
                    "margin",
                    snapshot.margin_available >= self.parameters.minimum_available_margin,
                    None
                    if snapshot.margin_available >= self.parameters.minimum_available_margin
                    else RiskReasonCode.INSUFFICIENT_MARGIN,
                    str(snapshot.margin_available),
                    str(self.parameters.minimum_available_margin),
                )
                leverage = exposure.gross / snapshot.equity
                self._check(
                    checks,
                    "leverage",
                    leverage <= self.parameters.max_leverage,
                    None
                    if leverage <= self.parameters.max_leverage
                    else RiskReasonCode.MAX_LEVERAGE_EXCEEDED,
                    str(leverage),
                    str(self.parameters.max_leverage),
                )
                self._check(
                    checks,
                    "daily_loss",
                    snapshot.daily_pnl > -self.parameters.daily_loss_limit,
                    None
                    if snapshot.daily_pnl > -self.parameters.daily_loss_limit
                    else RiskReasonCode.DAILY_LOSS_LIMIT,
                    str(snapshot.daily_pnl),
                    str(-self.parameters.daily_loss_limit),
                )
                drawdown = max(_ZERO, snapshot.high_water_mark - snapshot.equity)
                self._check(
                    checks,
                    "drawdown",
                    drawdown < self.parameters.max_drawdown,
                    None
                    if drawdown < self.parameters.max_drawdown
                    else RiskReasonCode.DRAWDOWN_LIMIT,
                    str(drawdown),
                    str(self.parameters.max_drawdown),
                )
                if action is RiskAction.NEW_OR_INCREASE:
                    self._check(
                        checks,
                        "position_count",
                        exposure.open_positions < self.parameters.max_open_positions,
                        None
                        if exposure.open_positions < self.parameters.max_open_positions
                        else RiskReasonCode.MAX_POSITIONS_EXCEEDED,
                        str(exposure.open_positions),
                        str(self.parameters.max_open_positions),
                    )
                    self._check(
                        checks,
                        "pending_intents",
                        snapshot.pending_intent_count < self.parameters.max_pending_intents,
                        None
                        if snapshot.pending_intent_count < self.parameters.max_pending_intents
                        else RiskReasonCode.MAX_PENDING_INTENTS_EXCEEDED,
                        str(snapshot.pending_intent_count),
                        str(self.parameters.max_pending_intents),
                    )
                    for check_id, remaining, reason in self._new_risk_limits(
                        exposure, intent.direction
                    ):
                        self._check(
                            checks,
                            check_id,
                            remaining > 0,
                            None if remaining > 0 else reason,
                            str(remaining),
                            ">0",
                        )
                else:
                    self._check(checks, "reduction_scope", abs(exposure.instrument) > 0)

        failed = tuple(item for item in checks if not item.passed)
        if failed or exposure is None or action is None:
            return self._decision(context, RiskDecisionStatus.REJECT, tuple(checks), None)

        if action is RiskAction.REDUCTION_ONLY:
            authorization = RiskAuthorization(
                action=action,
                max_new_notional=_ZERO,
                max_loss_at_stop=_ZERO,
                max_reduction_notional=abs(exposure.instrument),
            )
        else:
            available_limits = [
                item[1] for item in self._new_risk_limits(exposure, intent.direction)
            ]
            authorization = RiskAuthorization(
                action=action,
                max_new_notional=min(self.parameters.max_trade_notional, *available_limits),
                max_loss_at_stop=self.parameters.max_loss_at_stop,
                max_reduction_notional=_ZERO,
            )
        return self._decision(context, RiskDecisionStatus.APPROVE, tuple(checks), authorization)

    def _decision(
        self,
        context: RiskEvaluationContext,
        status: RiskDecisionStatus,
        checks: tuple[RiskCheckResult, ...],
        authorization: RiskAuthorization | None,
    ) -> RiskDecision:
        reasons = tuple(item.reason_code for item in checks if item.reason_code is not None)
        return RiskDecision(
            decision_id=deterministic_risk_decision_id(
                context=context,
                policy_id=self.policy_id,
                policy_version=self.policy_version,
                configuration_id=self.configuration_id,
                status=status,
                checks=checks,
                authorization=authorization,
            ),
            status=status,
            decision_timestamp=context.decision_timestamp,
            intent_id=context.intent.intent_id,
            instrument=context.intent.instrument,
            direction=context.intent.direction,
            policy_id=self.policy_id,
            policy_version=self.policy_version,
            configuration_id=self.configuration_id,
            reason_codes=reasons,
            checks=checks,
            authorization=authorization,
            regime_state_id=context.regime_state.state_id if context.regime_state else None,
            portfolio_snapshot_timestamp=(
                context.portfolio.timestamp if context.portfolio else None
            ),
            market_snapshot_timestamp=context.market.timestamp if context.market else None,
        )


__all__ = ["RiskFirewall"]
