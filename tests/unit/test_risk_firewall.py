"""Adversarial safety contracts for the Phase 7 deterministic risk firewall."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from traderos.data.instruments import AssetClass, Instrument
from traderos.data.timeframes import Timeframe
from traderos.regimes import (
    DataSessionState,
    LiquidityRegime,
    RegimeState,
    TrendRegime,
    VolatilityRegime,
)
from traderos.risk import (
    MarketDataStatus,
    MarketRiskSnapshot,
    PortfolioRiskSnapshot,
    RiskAction,
    RiskAuthorization,
    RiskCheckResult,
    RiskDecisionStatus,
    RiskEvaluationContext,
    RiskFirewall,
    RiskFirewallParameters,
    RiskLock,
    RiskPositionSnapshot,
    RiskReasonCode,
    SystemHealthSnapshot,
    SystemHealthStatus,
    risk_configuration_identity,
)
from traderos.signals import (
    FusionContext,
    MajorityVoteFusionPolicy,
    SignalFusionEngine,
)
from traderos.strategies import FeatureRequirement, SignalDirection, StrategySignal

BASE = datetime(2024, 1, 2, 12, tzinfo=UTC)


def instrument(symbol: str = "EUR/USD") -> Instrument:
    return Instrument(
        canonical_symbol=symbol,
        asset_class=AssetClass.FOREX,
        base_currency="EUR",
        quote_currency="USD",
        trading_currency="USD",
    )


def state(item: Instrument | None = None, **changes: object) -> RegimeState:
    item = item or instrument()
    values: dict[str, object] = {
        "state_id": "state-1",
        "instrument": item,
        "asset_class": item.asset_class,
        "timeframe": Timeframe.H1,
        "decision_timestamp": BASE,
        "regime_detector_id": "fixture",
        "regime_detector_version": "1",
        "configuration_id": "regime-config",
        "trend": TrendRegime.TRENDING_UP,
        "volatility": VolatilityRegime.NORMAL,
        "liquidity": LiquidityRegime.NORMAL,
        "data_session": DataSessionState.ACTIVE,
        "feature_provenance": (),
    }
    values.update(changes)
    return RegimeState(**values)  # type: ignore[arg-type]


def fused_intent(item: Instrument | None = None, direction: SignalDirection = SignalDirection.LONG):
    item = item or instrument()
    signal = StrategySignal(
        signal_id=f"signal-{direction.value}",
        strategy_id="fixture_strategy",
        strategy_version="1",
        instrument=item,
        timestamp=BASE,
        timeframe=Timeframe.H1,
        direction=direction,
        reason="fixture",
        feature_provenance=(FeatureRequirement("ema", 1),),
    )
    return SignalFusionEngine(MajorityVoteFusionPolicy()).fuse(
        FusionContext(
            instrument=item,
            timeframe=Timeframe.H1,
            decision_timestamp=BASE,
            signals=(signal,),
            regime_state=state(item),
        )
    )


def parameters(**changes: object) -> RiskFirewallParameters:
    values: dict[str, object] = {
        "max_market_data_age": timedelta(minutes=5),
        "max_risk_snapshot_age": timedelta(minutes=5),
        "max_system_health_age": timedelta(minutes=5),
        "max_spread_fraction": Decimal("0.01"),
        "max_trade_notional": Decimal("1000"),
        "max_loss_at_stop": Decimal("100"),
        "max_gross_exposure": Decimal("5000"),
        "max_net_exposure": Decimal("4000"),
        "max_long_exposure": Decimal("4000"),
        "max_short_exposure": Decimal("4000"),
        "max_instrument_exposure": Decimal("2000"),
        "max_asset_class_exposure": Decimal("4000"),
        "max_open_positions": 4,
        "max_pending_intents": 3,
        "max_leverage": Decimal("2"),
        "minimum_available_margin": Decimal("100"),
        "minimum_cash_buffer": Decimal("100"),
        "daily_loss_limit": Decimal("500"),
        "max_drawdown": Decimal("1000"),
    }
    values.update(changes)
    return RiskFirewallParameters(**values)  # type: ignore[arg-type]


def portfolio(**changes: object) -> PortfolioRiskSnapshot:
    values: dict[str, object] = {
        "timestamp": BASE,
        "account_id": "risk-test-account",
        "account_currency": "USD",
        "risk_day": date(2024, 1, 2),
        "equity": Decimal("10000"),
        "cash": Decimal("10000"),
        "margin_used": Decimal("0"),
        "margin_available": Decimal("10000"),
        "daily_pnl": Decimal("0"),
        "high_water_mark": Decimal("10000"),
        "positions": (),
    }
    values.update(changes)
    values["position_mark_timestamp"] = values.get("position_mark_timestamp", values["timestamp"])
    return PortfolioRiskSnapshot(**values)  # type: ignore[arg-type]


def context(**changes: object) -> RiskEvaluationContext:
    item = instrument()
    values: dict[str, object] = {
        "decision_timestamp": BASE,
        "intent": fused_intent(item),
        "regime_state": state(item),
        "market": MarketRiskSnapshot(
            instrument=item,
            timestamp=BASE,
            data_status=MarketDataStatus.VALID,
            bid=Decimal("100"),
            ask=Decimal("100.10"),
        ),
        "portfolio": portfolio(),
        "system_health": SystemHealthSnapshot(BASE, SystemHealthStatus.HEALTHY),
    }
    values.update(changes)
    return RiskEvaluationContext(**values)  # type: ignore[arg-type]


def decision(*, config: RiskFirewallParameters | None = None, **changes: object):
    return RiskFirewall(config or parameters()).evaluate(context(**changes))


def assert_rejected(result, reason: RiskReasonCode) -> None:
    assert result.status is RiskDecisionStatus.REJECT
    assert reason in result.reason_codes
    assert result.authorization is None


def test_approval_is_quantity_free_but_explicitly_bounded_and_idempotent() -> None:
    first = decision()
    second = decision()
    assert first.status is RiskDecisionStatus.APPROVE
    assert first.authorization is not None
    assert first.authorization.action is RiskAction.NEW_OR_INCREASE
    assert first.authorization.max_new_notional == Decimal("1000")
    assert first.authorization.max_loss_at_stop == Decimal("100")
    assert first.authorization.stop_risk_supported is False
    assert first.decision_id == second.decision_id
    assert first.checks == second.checks


def test_new_authorization_includes_leverage_headroom() -> None:
    result = decision(
        config=parameters(
            max_trade_notional=Decimal("5000"),
            max_gross_exposure=Decimal("50000"),
            max_net_exposure=Decimal("50000"),
            max_long_exposure=Decimal("50000"),
            max_instrument_exposure=Decimal("50000"),
            max_asset_class_exposure=Decimal("50000"),
        ),
        portfolio=portfolio(
            positions=(RiskPositionSnapshot(instrument("GBP/USD"), Decimal("19000")),)
        ),
    )
    assert result.status is RiskDecisionStatus.APPROVE
    assert result.authorization is not None
    assert result.authorization.max_new_notional == Decimal("1000")


def test_kill_switch_has_absolute_veto_authority() -> None:
    assert_rejected(
        decision(config=parameters(kill_switch_active=True)), RiskReasonCode.KILL_SWITCH_ACTIVE
    )


@pytest.mark.parametrize(
    "status",
    [SystemHealthStatus.DEGRADED, SystemHealthStatus.UNAVAILABLE, SystemHealthStatus.UNKNOWN],
)
def test_nonhealthy_system_status_fails_closed(status: SystemHealthStatus) -> None:
    assert_rejected(
        decision(system_health=SystemHealthSnapshot(BASE, status)), RiskReasonCode.SYSTEM_UNHEALTHY
    )


@pytest.mark.parametrize(
    ("timestamp", "reason"),
    [
        (BASE - timedelta(minutes=6), RiskReasonCode.DATA_STALE),
        (BASE + timedelta(seconds=1), RiskReasonCode.FUTURE_DATED_INPUT),
    ],
)
def test_market_freshness_rejects_stale_and_future_quotes(
    timestamp: datetime, reason: RiskReasonCode
) -> None:
    quote = replace(context().market, timestamp=timestamp)
    assert quote is not None
    assert_rejected(decision(market=quote), reason)


def test_stale_health_and_portfolio_snapshots_are_rejected() -> None:
    assert_rejected(
        decision(
            system_health=SystemHealthSnapshot(
                BASE - timedelta(minutes=6), SystemHealthStatus.HEALTHY
            )
        ),
        RiskReasonCode.SYSTEM_HEALTH_STALE,
    )
    assert_rejected(
        decision(portfolio=portfolio(timestamp=BASE - timedelta(minutes=6))),
        RiskReasonCode.STALE_RISK_SNAPSHOT,
    )


def test_future_health_and_portfolio_snapshots_are_rejected() -> None:
    assert_rejected(
        decision(
            system_health=SystemHealthSnapshot(
                BASE + timedelta(seconds=1), SystemHealthStatus.HEALTHY
            )
        ),
        RiskReasonCode.FUTURE_DATED_INPUT,
    )
    assert_rejected(
        decision(portfolio=portfolio(timestamp=BASE + timedelta(seconds=1))),
        RiskReasonCode.FUTURE_DATED_INPUT,
    )


def test_missing_required_safety_snapshots_fail_closed() -> None:
    assert_rejected(decision(market=None), RiskReasonCode.DATA_UNAVAILABLE)
    assert_rejected(decision(portfolio=None), RiskReasonCode.PORTFOLIO_INVALID)
    assert_rejected(decision(system_health=None), RiskReasonCode.SYSTEM_UNHEALTHY)


@pytest.mark.parametrize(
    ("bid", "ask", "reason"),
    [
        (None, Decimal("100"), RiskReasonCode.PRICE_INVALID),
        (Decimal("0"), Decimal("100"), RiskReasonCode.PRICE_INVALID),
        (Decimal("101"), Decimal("100"), RiskReasonCode.CROSSED_MARKET),
        (Decimal("100"), Decimal("102"), RiskReasonCode.SPREAD_TOO_WIDE),
        (Decimal("NaN"), Decimal("100"), RiskReasonCode.PRICE_INVALID),
        (Decimal("Infinity"), Decimal("100"), RiskReasonCode.PRICE_INVALID),
    ],
)
def test_invalid_prices_and_spreads_fail_closed(
    bid: Decimal | None, ask: Decimal, reason: RiskReasonCode
) -> None:
    quote = MarketRiskSnapshot(instrument(), BASE, MarketDataStatus.VALID, bid, ask)
    assert_rejected(decision(market=quote), reason)


def test_spread_equality_is_allowed_and_just_over_is_rejected() -> None:
    at_limit = MarketRiskSnapshot(
        instrument(), BASE, MarketDataStatus.VALID, Decimal("100"), Decimal("102")
    )
    # Midpoint convention makes this 2/101, so use the exact Decimal threshold instead.
    config = parameters(max_spread_fraction=Decimal("2") / Decimal("101"))
    assert decision(config=config, market=at_limit).status is RiskDecisionStatus.APPROVE
    assert_rejected(
        decision(config=parameters(max_spread_fraction=Decimal("0.019")), market=at_limit),
        RiskReasonCode.SPREAD_TOO_WIDE,
    )


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (MarketDataStatus.STALE, RiskReasonCode.DATA_STALE),
        (MarketDataStatus.INVALID, RiskReasonCode.DATA_INVALID),
        (MarketDataStatus.UNAVAILABLE, RiskReasonCode.DATA_UNAVAILABLE),
    ],
)
def test_explicit_market_data_quality_states_fail_closed(
    status: MarketDataStatus, reason: RiskReasonCode
) -> None:
    quote = replace(context().market, data_status=status)
    assert quote is not None
    assert_rejected(decision(market=quote), reason)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"data_session": DataSessionState.INACTIVE}, RiskReasonCode.MARKET_INACTIVE),
        ({"liquidity": LiquidityRegime.UNKNOWN}, RiskReasonCode.REGIME_UNAVAILABLE),
        ({"liquidity": LiquidityRegime.STRESSED}, RiskReasonCode.LIQUIDITY_STRESSED),
        ({"trend": TrendRegime.UNAVAILABLE}, RiskReasonCode.REGIME_UNAVAILABLE),
        ({"volatility": VolatilityRegime.UNAVAILABLE}, RiskReasonCode.REGIME_UNAVAILABLE),
    ],
)
def test_regime_gates_are_explicit_and_fail_closed(
    changes: dict[str, object], reason: RiskReasonCode
) -> None:
    assert_rejected(decision(regime_state=state(**changes)), reason)


def test_regime_identity_mismatch_missing_regime_and_future_regime_are_rejected() -> None:
    assert_rejected(decision(regime_state=None), RiskReasonCode.REGIME_UNAVAILABLE)
    assert_rejected(
        decision(regime_state=state(state_id="other")), RiskReasonCode.REGIME_UNAVAILABLE
    )
    assert_rejected(
        decision(regime_state=state(decision_timestamp=BASE + timedelta(seconds=1))),
        RiskReasonCode.FUTURE_DATED_INPUT,
    )
    assert_rejected(
        decision(regime_state=state(decision_timestamp=BASE - timedelta(seconds=1))),
        RiskReasonCode.REGIME_UNAVAILABLE,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("equity", None),
        ("equity", Decimal("-1")),
        ("equity", Decimal("Infinity")),
        ("cash", Decimal("NaN")),
        ("margin_available", Decimal("-1")),
        ("margin_used", Decimal("-1")),
        ("daily_pnl", Decimal("NaN")),
        ("high_water_mark", Decimal("0")),
    ],
)
def test_missing_negative_and_nonfinite_portfolio_values_fail_closed(
    field: str, value: Decimal | None
) -> None:
    assert_rejected(
        decision(portfolio=portfolio(**{field: value})), RiskReasonCode.PORTFOLIO_INVALID
    )


def test_capital_margin_leverage_daily_loss_drawdown_and_lock_limits() -> None:
    assert_rejected(
        decision(portfolio=portfolio(cash=Decimal("99"))), RiskReasonCode.INSUFFICIENT_CAPITAL
    )
    assert_rejected(
        decision(portfolio=portfolio(margin_available=Decimal("99"))),
        RiskReasonCode.INSUFFICIENT_MARGIN,
    )
    leveraged = portfolio(
        positions=(RiskPositionSnapshot(instrument("GBP/USD"), Decimal("30000")),)
    )
    assert_rejected(decision(portfolio=leveraged), RiskReasonCode.MAX_LEVERAGE_EXCEEDED)
    assert_rejected(
        decision(portfolio=portfolio(daily_pnl=Decimal("-500"))), RiskReasonCode.DAILY_LOSS_LIMIT
    )
    assert_rejected(
        decision(portfolio=portfolio(equity=Decimal("9000"))), RiskReasonCode.DRAWDOWN_LIMIT
    )
    assert_rejected(
        decision(portfolio=portfolio(active_locks=frozenset({RiskLock.DRAWDOWN}))),
        RiskReasonCode.DRAWDOWN_LOCKED,
    )
    assert_rejected(
        decision(portfolio=portfolio(active_locks=frozenset({RiskLock.MANUAL}))),
        RiskReasonCode.RISK_LOCK_ACTIVE,
    )
    assert_rejected(
        decision(portfolio=portfolio(risk_day=date(2024, 1, 1))),
        RiskReasonCode.RISK_DAY_MISMATCH,
    )


@pytest.mark.parametrize(
    ("position", "config_change", "reason"),
    [
        (
            RiskPositionSnapshot(instrument("GBP/USD"), Decimal("5000")),
            {},
            RiskReasonCode.MAX_GROSS_EXPOSURE_EXCEEDED,
        ),
        (
            RiskPositionSnapshot(instrument("GBP/USD"), Decimal("4000")),
            {},
            RiskReasonCode.MAX_NET_EXPOSURE_EXCEEDED,
        ),
        (
            RiskPositionSnapshot(instrument("GBP/USD"), Decimal("4000")),
            {},
            RiskReasonCode.MAX_LONG_EXPOSURE_EXCEEDED,
        ),
        (
            RiskPositionSnapshot(instrument(), Decimal("2000")),
            {},
            RiskReasonCode.MAX_INSTRUMENT_EXPOSURE_EXCEEDED,
        ),
        (
            RiskPositionSnapshot(instrument("GBP/USD"), Decimal("4000")),
            {},
            RiskReasonCode.MAX_ASSET_CLASS_EXPOSURE_EXCEEDED,
        ),
    ],
)
def test_exposure_and_concentration_limits_reject_new_risk(
    position: RiskPositionSnapshot, config_change: dict[str, object], reason: RiskReasonCode
) -> None:
    assert_rejected(
        decision(config=parameters(**config_change), portfolio=portfolio(positions=(position,))),
        reason,
    )


def test_open_position_pending_and_duplicate_limits_reject_new_risk() -> None:
    positions = tuple(
        RiskPositionSnapshot(instrument(f"EUR/{index}"), Decimal("100")) for index in range(4)
    )
    assert_rejected(
        decision(portfolio=portfolio(positions=positions)), RiskReasonCode.MAX_POSITIONS_EXCEEDED
    )
    assert_rejected(
        decision(portfolio=portfolio(pending_intent_count=3)),
        RiskReasonCode.MAX_PENDING_INTENTS_EXCEEDED,
    )
    intent = context().intent
    assert_rejected(
        decision(portfolio=portfolio(reserved_intent_ids=frozenset({intent.intent_id}))),
        RiskReasonCode.DUPLICATE_INTENT,
    )


def test_opposing_intent_is_constrained_to_safe_reduction_not_a_reversal() -> None:
    result = decision(
        portfolio=portfolio(positions=(RiskPositionSnapshot(instrument(), Decimal("-750")),))
    )
    assert result.status is RiskDecisionStatus.APPROVE
    assert result.authorization is not None
    assert result.authorization.action is RiskAction.REDUCTION_ONLY
    assert result.authorization.max_new_notional == Decimal("0")
    assert result.authorization.max_reduction_notional == Decimal("750")


@pytest.mark.parametrize("direction", [SignalDirection.FLAT, SignalDirection.HOLD])
def test_non_actionable_fusion_semantics_cannot_create_risk(direction: SignalDirection) -> None:
    assert_rejected(
        decision(intent=fused_intent(direction=direction)), RiskReasonCode.INTENT_NOT_ACTIONABLE
    )


def test_future_append_and_mutation_cannot_change_historical_decision() -> None:
    historical = decision()
    future_quote = replace(
        context().market, timestamp=BASE + timedelta(hours=1), ask=Decimal("999")
    )
    assert future_quote is not None
    future = decision(market=future_quote)
    assert_rejected(future, RiskReasonCode.FUTURE_DATED_INPUT)
    assert decision() == historical


def test_future_dated_unified_intent_is_rejected_before_any_risk_authorization() -> None:
    future_intent = replace(context().intent, decision_timestamp=BASE + timedelta(seconds=1))
    assert_rejected(decision(intent=future_intent), RiskReasonCode.FUTURE_DATED_INPUT)


def test_position_permutation_does_not_change_decision_or_identity() -> None:
    first_positions = (
        RiskPositionSnapshot(instrument("GBP/USD"), Decimal("100")),
        RiskPositionSnapshot(instrument("USD/JPY"), Decimal("-50")),
    )
    first = decision(portfolio=portfolio(positions=first_positions))
    second = decision(portfolio=portfolio(positions=tuple(reversed(first_positions))))
    assert first == second


def test_configuration_identity_and_validation_are_strict_and_deterministic() -> None:
    baseline = parameters()
    changed = parameters(max_trade_notional=Decimal("999"))
    assert risk_configuration_identity(baseline) == risk_configuration_identity(baseline)
    assert risk_configuration_identity(baseline) != risk_configuration_identity(changed)
    with pytest.raises(ValidationError):
        RiskFirewallParameters(max_trade_notional=Decimal("0"))
    with pytest.raises(ValidationError):
        RiskFirewallParameters(max_market_data_age=timedelta(0))
    with pytest.raises(ValidationError):
        RiskFirewallParameters(minimum_cash_buffer=Decimal("-1"))
    with pytest.raises(ValidationError):
        RiskFirewallParameters(max_open_positions=0)
    with pytest.raises(ValidationError):
        RiskFirewallParameters(unexpected=True)  # type: ignore[call-arg]


def test_risk_contract_models_reject_malformed_audit_and_authorization_values() -> None:
    with pytest.raises(ValueError):
        portfolio(account_currency="")
    with pytest.raises(ValueError):
        portfolio(pending_intent_count=-1)
    duplicate = RiskPositionSnapshot(instrument(), Decimal("1"))
    with pytest.raises(ValueError):
        portfolio(positions=(duplicate, duplicate))
    with pytest.raises(ValueError):
        portfolio(reserved_intent_ids=frozenset({""}))
    with pytest.raises(ValueError):
        RiskCheckResult("", True)
    with pytest.raises(ValueError):
        RiskCheckResult("x", True, RiskReasonCode.DATA_INVALID)
    with pytest.raises(ValueError):
        RiskAuthorization(RiskAction.NEW_OR_INCREASE, Decimal("0"), Decimal("1"), Decimal("0"))
    with pytest.raises(ValueError):
        RiskAuthorization(RiskAction.REDUCTION_ONLY, Decimal("1"), Decimal("0"), Decimal("1"))
    with pytest.raises(ValueError):
        RiskAuthorization(RiskAction.REDUCTION_ONLY, Decimal("NaN"), Decimal("0"), Decimal("1"))


def test_risk_decision_model_cannot_misrepresent_approval_or_rejection() -> None:
    approved = decision()
    rejected = decision(config=parameters(kill_switch_active=True))
    with pytest.raises(ValueError):
        replace(approved, decision_id="")
    with pytest.raises(ValueError):
        replace(approved, reason_codes=(RiskReasonCode.DATA_INVALID,))
    with pytest.raises(ValueError):
        replace(rejected, reason_codes=())
    assert approved.authorization is not None
    with pytest.raises(ValueError):
        replace(rejected, authorization=approved.authorization)
