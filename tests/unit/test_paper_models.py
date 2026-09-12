"""Focused Decimal/domain invariants for the Phase 8 paper contracts."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from traderos.backtesting.models import OrderSide, OrderType, TimeInForce
from traderos.data.errors import TimestampNormalizationError
from traderos.data.instruments import AssetClass, Instrument
from traderos.paper import (
    PaperAccount,
    PaperExecutionConfig,
    PaperFill,
    PaperOrder,
    PaperOrderStatus,
    PaperPosition,
    PaperQuote,
    PaperTradingError,
    quantity_for_authorization,
)
from traderos.risk import (
    RiskAction,
    RiskAuthorization,
    RiskDecision,
    RiskDecisionStatus,
    RiskReasonCode,
    risk_decision_integrity_id,
)
from traderos.signals import FusionDirection

NOW = datetime(2024, 1, 2, 12, tzinfo=UTC)


def instrument(*, currency: str = "USD") -> Instrument:
    return Instrument(
        canonical_symbol="EUR/USD",
        asset_class=AssetClass.FOREX,
        base_currency="EUR",
        quote_currency="USD",
        trading_currency=currency,
    )


def account(**changes: object) -> PaperAccount:
    values: dict[str, object] = {
        "account_id": "paper-model",
        "account_currency": "USD",
        "starting_cash": Decimal("1000"),
        "cash": Decimal("1000"),
        "reserved_risk": Decimal("0"),
        "risk_capacity": Decimal("500"),
        "daily_loss_limit": Decimal("100"),
        "max_drawdown": Decimal("200"),
        "risk_policy_id": "risk_firewall",
        "risk_policy_configuration_id": "config",
        "risk_day": date(2024, 1, 2),
        "risk_day_starting_equity": Decimal("1000"),
        "high_water_mark": Decimal("1000"),
        "realized_pnl": Decimal("0"),
        "fees": Decimal("0"),
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return PaperAccount(**values)  # type: ignore[arg-type]


def decision(**changes: object) -> RiskDecision:
    values: dict[str, object] = {
        "decision_id": "decision-model",
        "status": RiskDecisionStatus.APPROVE,
        "decision_timestamp": NOW,
        "intent_id": "intent-model",
        "account_id": "paper-model",
        "instrument": instrument(),
        "direction": FusionDirection.LONG,
        "policy_id": "risk_firewall",
        "policy_version": "1",
        "configuration_id": "config",
        "reason_codes": (),
        "checks": (),
        "authorization": RiskAuthorization(
            action=RiskAction.NEW_OR_INCREASE,
            max_new_notional=Decimal("500"),
            max_loss_at_stop=Decimal("50"),
            max_reduction_notional=Decimal("0"),
        ),
        "regime_state_id": "state",
        "portfolio_snapshot_timestamp": NOW,
        "market_snapshot_timestamp": NOW,
    }
    values.update(changes)
    value = RiskDecision(**values)  # type: ignore[arg-type]
    return replace(value, decision_id=risk_decision_integrity_id(value))


def quote(**changes: object) -> PaperQuote:
    values: dict[str, object] = {
        "instrument": instrument(),
        "timestamp": NOW,
        "bid": Decimal("100"),
        "ask": Decimal("101"),
    }
    values.update(changes)
    return PaperQuote(**values)  # type: ignore[arg-type]


def order(**changes: object) -> PaperOrder:
    values: dict[str, object] = {
        "order_id": "order-model",
        "idempotency_key": "request-model",
        "account_id": "paper-model",
        "instrument": instrument(),
        "side": OrderSide.BUY,
        "order_type": OrderType.MARKET,
        "quantity": Decimal("2"),
        "filled_quantity": Decimal("0"),
        "average_fill_price": None,
        "limit_price": None,
        "stop_price": None,
        "time_in_force": TimeInForce.GTC,
        "created_at": NOW,
        "submitted_at": NOW,
        "expires_at": None,
        "last_market_event_at": None,
        "risk_decision_id": "decision-model",
        "risk_policy_id": "risk_firewall",
        "risk_policy_configuration_id": "config",
        "sizing_configuration_id": "sizing-config",
        "source_intent_id": "intent-model",
        "authorization_action": RiskAction.NEW_OR_INCREASE,
        "status": PaperOrderStatus.ACCEPTED,
    }
    values.update(changes)
    return PaperOrder(**values)  # type: ignore[arg-type]


def test_configuration_is_strict_deterministic_and_rejects_bad_decimal_values() -> None:
    first = PaperExecutionConfig()
    assert first.configuration_id == PaperExecutionConfig().configuration_id
    with pytest.raises(ValidationError):
        PaperExecutionConfig(decision_max_age=timedelta(0))
    with pytest.raises(ValidationError):
        PaperExecutionConfig(quantity_increment=Decimal("NaN"))
    with pytest.raises(ValidationError):
        PaperExecutionConfig(commission_rate=Decimal("-0.1"))
    with pytest.raises(ValidationError):
        PaperExecutionConfig(max_fill_quantity=Decimal("0"))


def test_quote_account_position_and_fill_reject_invalid_numeric_or_temporal_state() -> None:
    assert quote().midpoint == Decimal("100.5")
    with pytest.raises(PaperTradingError, match="crossed"):
        quote(bid=Decimal("102"))
    with pytest.raises(PaperTradingError, match="positive"):
        quote(ask=Decimal("0"))
    with pytest.raises(TimestampNormalizationError):
        quote(timestamp=NOW.replace(tzinfo=None))
    with pytest.raises(PaperTradingError, match="capital"):
        account(risk_capacity=Decimal("0"))
    with pytest.raises(PaperTradingError, match="identity"):
        account(account_id=" ")
    with pytest.raises(PaperTradingError, match="finite"):
        account(cash=Decimal("NaN"))
    with pytest.raises(PaperTradingError, match="prices"):
        PaperPosition(
            "paper-model",
            instrument(),
            Decimal("1"),
            Decimal("-1"),
            Decimal("1"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            NOW,
        )
    with pytest.raises(PaperTradingError, match="finite"):
        PaperPosition(
            "paper-model",
            instrument(),
            Decimal("NaN"),
            Decimal("1"),
            Decimal("1"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            NOW,
        )
    with pytest.raises(PaperTradingError, match="commission"):
        PaperFill(
            "fill",
            "order",
            "paper-model",
            instrument(),
            OrderSide.BUY,
            Decimal("1"),
            Decimal("1"),
            Decimal("1"),
            Decimal("-1"),
            NOW,
        )
    with pytest.raises(PaperTradingError, match="identity"):
        PaperFill(
            " ",
            "order",
            "paper-model",
            instrument(),
            OrderSide.BUY,
            Decimal("1"),
            Decimal("1"),
            Decimal("1"),
            Decimal("0"),
            NOW,
        )


def test_order_contract_and_state_machine_reject_impossible_lifecycle_states() -> None:
    accepted = order()
    assert accepted.remaining_quantity == Decimal("2")
    assert accepted.is_open
    assert accepted.can_transition_to(PaperOrderStatus.PARTIALLY_FILLED)
    assert not accepted.can_transition_to(PaperOrderStatus.CREATED)
    filled = replace(accepted, status=PaperOrderStatus.FILLED, filled_quantity=Decimal("2"))
    assert not filled.is_open
    assert not filled.can_transition_to(PaperOrderStatus.ACCEPTED)
    with pytest.raises(PaperTradingError, match="limit"):
        order(order_type=OrderType.LIMIT)
    with pytest.raises(PaperTradingError, match="stop"):
        order(order_type=OrderType.STOP)
    with pytest.raises(PaperTradingError, match="filled quantity"):
        order(filled_quantity=Decimal("3"))
    with pytest.raises(PaperTradingError, match="identity"):
        order(order_id=" ")
    with pytest.raises(PaperTradingError, match="order price"):
        order(average_fill_price=Decimal("0"))


def test_sizing_is_decimal_bounded_and_reduction_only_never_increases_risk() -> None:
    result = quantity_for_authorization(
        decision=decision(),
        quote=quote(),
        account=account(),
        position=None,
        reserved_risk=Decimal("0"),
        reserved_cash=Decimal("0"),
        config=PaperExecutionConfig(),
    )
    assert result.quantity * quote().ask <= Decimal("500")
    assert result.reserved_risk == result.reserved_cash
    with pytest.raises(PaperTradingError, match="reserved risk"):
        quantity_for_authorization(
            decision=decision(),
            quote=quote(),
            account=account(),
            position=None,
            reserved_risk=Decimal("-1"),
            reserved_cash=Decimal("0"),
            config=PaperExecutionConfig(),
        )
    foreign = Instrument(
        canonical_symbol="USD/EUR",
        asset_class=AssetClass.FOREX,
        base_currency="USD",
        quote_currency="EUR",
        trading_currency="EUR",
    )
    with pytest.raises(PaperTradingError, match="same-currency"):
        quantity_for_authorization(
            decision=decision(instrument=foreign),
            quote=quote(instrument=foreign),
            account=account(),
            position=None,
            reserved_risk=Decimal("0"),
            reserved_cash=Decimal("0"),
            config=PaperExecutionConfig(),
        )
    reduced = decision(
        direction=FusionDirection.SHORT,
        authorization=RiskAuthorization(
            action=RiskAction.REDUCTION_ONLY,
            max_new_notional=Decimal("0"),
            max_loss_at_stop=Decimal("0"),
            max_reduction_notional=Decimal("200"),
        ),
    )
    position = PaperPosition(
        "paper-model",
        instrument(),
        Decimal("1"),
        Decimal("100"),
        Decimal("100"),
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
        NOW,
    )
    reduced_result = quantity_for_authorization(
        decision=reduced,
        quote=quote(),
        account=account(),
        position=position,
        reserved_risk=Decimal("0"),
        reserved_cash=Decimal("0"),
        config=PaperExecutionConfig(),
    )
    assert reduced_result.reserved_risk == Decimal("0")
    assert reduced_result.quantity <= abs(position.quantity)
    with pytest.raises(PaperTradingError, match="reducible"):
        quantity_for_authorization(
            decision=reduced,
            quote=quote(),
            account=account(),
            position=None,
            reserved_risk=Decimal("0"),
            reserved_cash=Decimal("0"),
            config=PaperExecutionConfig(),
        )


def test_sizing_rejects_rejected_decisions_cash_exhaustion_and_quantity_bounds() -> None:
    rejected = replace(
        decision(),
        status=RiskDecisionStatus.REJECT,
        authorization=None,
        reason_codes=(RiskReasonCode.KILL_SWITCH_ACTIVE,),
    )
    with pytest.raises(PaperTradingError, match="rejected"):
        quantity_for_authorization(
            decision=rejected,
            quote=quote(),
            account=account(),
            position=None,
            reserved_risk=Decimal("0"),
            reserved_cash=Decimal("0"),
            config=PaperExecutionConfig(),
        )
    with pytest.raises(PaperTradingError, match="reserved cash"):
        quantity_for_authorization(
            decision=decision(),
            quote=quote(),
            account=account(),
            position=None,
            reserved_risk=Decimal("0"),
            reserved_cash=Decimal("-1"),
            config=PaperExecutionConfig(),
        )
    with pytest.raises(PaperTradingError, match="gross exposure"):
        quantity_for_authorization(
            decision=decision(),
            quote=quote(),
            account=account(),
            position=None,
            reserved_risk=Decimal("0"),
            reserved_cash=Decimal("0"),
            config=PaperExecutionConfig(),
            current_gross_exposure=Decimal("NaN"),
        )
    with pytest.raises(PaperTradingError, match="risk capacity"):
        quantity_for_authorization(
            decision=decision(),
            quote=quote(),
            account=account(),
            position=None,
            reserved_risk=Decimal("500"),
            reserved_cash=Decimal("0"),
            config=PaperExecutionConfig(),
        )
    with pytest.raises(PaperTradingError, match="risk capacity"):
        quantity_for_authorization(
            decision=decision(),
            quote=quote(),
            account=account(cash=Decimal("0")),
            position=None,
            reserved_risk=Decimal("0"),
            reserved_cash=Decimal("0"),
            config=PaperExecutionConfig(),
        )
    with pytest.raises(PaperTradingError, match="configured bounds"):
        quantity_for_authorization(
            decision=decision(),
            quote=quote(),
            account=account(),
            position=None,
            reserved_risk=Decimal("0"),
            reserved_cash=Decimal("0"),
            config=PaperExecutionConfig(maximum_quantity=Decimal("1")),
        )
    with pytest.raises(PaperTradingError, match="differ"):
        quantity_for_authorization(
            decision=decision(),
            quote=quote(
                instrument=Instrument(
                    canonical_symbol="USD/JPY",
                    asset_class=AssetClass.FOREX,
                    quote_currency="USD",
                    trading_currency="USD",
                )
            ),
            account=account(),
            position=None,
            reserved_risk=Decimal("0"),
            reserved_cash=Decimal("0"),
            config=PaperExecutionConfig(),
        )
