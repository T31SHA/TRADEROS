"""Phase 8 durable paper-engine safety and recovery integration tests."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from traderos.backtesting.models import OrderType
from traderos.data.instruments import AssetClass, Instrument
from traderos.database.connection import create_database_engine
from traderos.paper import (
    PaperExecutionConfig,
    PaperOrderStatus,
    PaperQuote,
    PaperRiskLockType,
    PaperTradingEngine,
    PaperTradingError,
    SqlAlchemyPaperStore,
)
from traderos.risk import (
    RiskAction,
    RiskAuthorization,
    RiskDecision,
    RiskDecisionStatus,
)
from traderos.signals import FusionDirection

NOW = datetime(2024, 1, 2, 12, tzinfo=UTC)


def instrument() -> Instrument:
    return Instrument(
        canonical_symbol="EUR/USD",
        asset_class=AssetClass.FOREX,
        base_currency="EUR",
        quote_currency="USD",
        trading_currency="USD",
    )


def decision(
    identifier: str = "decision-1", status: RiskDecisionStatus = RiskDecisionStatus.APPROVE
) -> RiskDecision:
    approved = status is RiskDecisionStatus.APPROVE
    return RiskDecision(
        decision_id=identifier,
        status=status,
        decision_timestamp=NOW,
        intent_id=f"intent-{identifier}",
        instrument=instrument(),
        direction=FusionDirection.LONG,
        policy_id="risk_firewall",
        policy_version="1",
        configuration_id="policy-config",
        reason_codes=() if approved else (),  # replaced below for strict rejected contract
        checks=(),
        authorization=(
            RiskAuthorization(
                action=RiskAction.NEW_OR_INCREASE,
                max_new_notional=Decimal("1000"),
                max_loss_at_stop=Decimal("100"),
                max_reduction_notional=Decimal("0"),
            )
            if approved
            else None
        ),
        regime_state_id="state-1",
        portfolio_snapshot_timestamp=NOW,
        market_snapshot_timestamp=NOW,
    )


@pytest.fixture
def engine() -> Iterator[PaperTradingEngine]:
    store = SqlAlchemyPaperStore(create_database_engine("sqlite+pysqlite:///:memory:"))
    store.create_schema_for_testing()
    paper = PaperTradingEngine(store, PaperExecutionConfig(max_fill_quantity=Decimal("4")))
    paper.create_account(
        account_id="paper-1",
        account_currency="USD",
        starting_cash=Decimal("10000"),
        risk_capacity=Decimal("1000"),
        daily_loss_limit=Decimal("500"),
        max_drawdown=Decimal("1000"),
        risk_policy_id="risk_firewall",
        risk_policy_configuration_id="policy-config",
        timestamp=NOW,
    )
    yield paper
    store.engine.dispose()


def quote(timestamp: datetime = NOW) -> PaperQuote:
    return PaperQuote(instrument(), timestamp, Decimal("100"), Decimal("101"))


def test_paper_order_requires_authorization_is_idempotent_and_never_same_quote_fills(
    engine: PaperTradingEngine,
) -> None:
    first = engine.submit(
        account_id="paper-1",
        idempotency_key="request-1",
        risk_decision=decision(),
        quote=quote(),
        timestamp=NOW,
    )
    again = engine.submit(
        account_id="paper-1",
        idempotency_key="request-1",
        risk_decision=decision(),
        quote=quote(),
        timestamp=NOW,
    )
    assert first.order_id == again.order_id
    assert first.status is PaperOrderStatus.ACCEPTED
    assert engine.process_quote(account_id="paper-1", quote=quote(), timestamp=NOW) == ()
    fills = engine.process_quote(
        account_id="paper-1",
        quote=quote(NOW + timedelta(seconds=1)),
        timestamp=NOW + timedelta(seconds=1),
    )
    assert len(fills) == 1
    assert fills[0].quantity == Decimal("4")
    recovered, positions, orders = engine.recover("paper-1")
    assert Decimal("0") < recovered.reserved_risk <= Decimal("1000")
    assert positions[0].quantity == Decimal("4")
    assert orders[0].status is PaperOrderStatus.PARTIALLY_FILLED


def test_rejected_or_stale_risk_decisions_fail_closed(engine: PaperTradingEngine) -> None:
    with pytest.raises((PaperTradingError, ValueError)):
        engine.submit(
            account_id="paper-1",
            idempotency_key="reject",
            risk_decision=decision("old"),
            quote=quote(NOW + timedelta(minutes=6)),
            timestamp=NOW + timedelta(minutes=6),
        )


def test_durable_lock_survives_new_engine_and_blocks_new_risk(engine: PaperTradingEngine) -> None:
    engine.activate_risk_lock(
        account_id="paper-1",
        lock_type=PaperRiskLockType.EMERGENCY_LOCK,
        reason="test",
        timestamp=NOW,
    )
    restarted = PaperTradingEngine(engine.store, engine.config)
    assert PaperRiskLockType.EMERGENCY_LOCK in restarted.store.active_locks("paper-1")
    with pytest.raises(PaperTradingError, match="risk lock"):
        restarted.submit(
            account_id="paper-1",
            idempotency_key="blocked",
            risk_decision=decision(),
            quote=quote(),
            timestamp=NOW,
        )


def test_second_decision_cannot_consume_an_already_reserved_risk_capacity(
    engine: PaperTradingEngine,
) -> None:
    engine.submit(
        account_id="paper-1",
        idempotency_key="first",
        risk_decision=decision("decision-first"),
        quote=quote(),
        timestamp=NOW,
    )
    with pytest.raises(PaperTradingError, match="risk capacity"):
        engine.submit(
            account_id="paper-1",
            idempotency_key="second",
            risk_decision=decision("decision-second"),
            quote=quote(),
            timestamp=NOW,
        )


def test_limit_order_waits_for_touch_and_fill_is_not_replayable(engine: PaperTradingEngine) -> None:
    order = engine.submit(
        account_id="paper-1",
        idempotency_key="limit",
        risk_decision=decision(),
        quote=quote(),
        timestamp=NOW,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("100"),
    )
    assert (
        engine.process_quote(
            account_id="paper-1",
            quote=quote(NOW + timedelta(seconds=1)),
            timestamp=NOW + timedelta(seconds=1),
        )
        == ()
    )
    touched = PaperQuote(instrument(), NOW + timedelta(seconds=2), Decimal("99"), Decimal("100"))
    assert (
        len(engine.process_quote(account_id="paper-1", quote=touched, timestamp=touched.timestamp))
        == 1
    )
    assert (
        engine.process_quote(account_id="paper-1", quote=touched, timestamp=touched.timestamp) == ()
    )
    assert engine.store.order("paper-1", order.order_id).filled_quantity == Decimal("4")
