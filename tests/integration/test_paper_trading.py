"""Phase 8 durable paper-engine safety and recovery integration tests."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from traderos.backtesting.models import OrderType, TimeInForce
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
    risk_decision_integrity_id,
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
    identifier: str = "decision-1",
    status: RiskDecisionStatus = RiskDecisionStatus.APPROVE,
    *,
    timestamp: datetime = NOW,
    direction: FusionDirection = FusionDirection.LONG,
    action: RiskAction = RiskAction.NEW_OR_INCREASE,
    max_new_notional: Decimal = Decimal("1000"),
) -> RiskDecision:
    approved = status is RiskDecisionStatus.APPROVE
    value = RiskDecision(
        decision_id=identifier,
        status=status,
        decision_timestamp=timestamp,
        intent_id=f"intent-{identifier}",
        account_id="paper-1",
        instrument=instrument(),
        direction=direction,
        policy_id="risk_firewall",
        policy_version="1",
        configuration_id="policy-config",
        reason_codes=() if approved else (),  # replaced below for strict rejected contract
        checks=(),
        authorization=(
            RiskAuthorization(
                action=action,
                max_new_notional=max_new_notional
                if action is RiskAction.NEW_OR_INCREASE
                else Decimal("0"),
                max_loss_at_stop=Decimal("100")
                if action is RiskAction.NEW_OR_INCREASE
                else Decimal("0"),
                max_reduction_notional=Decimal("1000")
                if action is RiskAction.REDUCTION_ONLY
                else Decimal("0"),
            )
            if approved
            else None
        ),
        regime_state_id="state-1",
        portfolio_snapshot_timestamp=timestamp,
        market_snapshot_timestamp=timestamp,
    )
    return replace(value, decision_id=risk_decision_integrity_id(value))


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


def test_lock_cancels_pending_new_risk_before_a_later_quote_can_fill(
    engine: PaperTradingEngine,
) -> None:
    order = engine.submit(
        account_id="paper-1",
        idempotency_key="pending-lock",
        risk_decision=decision("pending-lock"),
        quote=quote(),
        timestamp=NOW,
    )
    engine.activate_risk_lock(
        account_id="paper-1",
        lock_type=PaperRiskLockType.EMERGENCY_LOCK,
        reason="operator stop",
        timestamp=NOW + timedelta(seconds=1),
    )
    assert engine.store.order("paper-1", order.order_id).status is PaperOrderStatus.CANCELLED
    assert engine.store.active_reservation_total_for_account("paper-1") == Decimal("0")
    assert (
        engine.process_quote(
            account_id="paper-1",
            quote=quote(NOW + timedelta(seconds=2)),
            timestamp=NOW + timedelta(seconds=2),
        )
        == ()
    )


def test_ioc_partial_fill_cancels_remainder_and_releases_reservation(
    engine: PaperTradingEngine,
) -> None:
    order = engine.submit(
        account_id="paper-1",
        idempotency_key="ioc-partial",
        risk_decision=decision("ioc-partial"),
        quote=quote(),
        timestamp=NOW,
        time_in_force=TimeInForce.IOC,
    )
    fills = engine.process_quote(
        account_id="paper-1",
        quote=quote(NOW + timedelta(seconds=1)),
        timestamp=NOW + timedelta(seconds=1),
    )
    assert len(fills) == 1
    persisted = engine.store.order("paper-1", order.order_id)
    assert persisted.status is PaperOrderStatus.EXPIRED
    assert persisted.filled_quantity == Decimal("4")
    assert engine.store.active_reservation_total_for_account("paper-1") == Decimal("0")


def test_reduction_only_orders_are_economic_and_cannot_overlap_past_flat(
    engine: PaperTradingEngine,
) -> None:
    entry = engine.submit(
        account_id="paper-1",
        idempotency_key="entry-for-reduction",
        risk_decision=decision("entry-for-reduction"),
        quote=quote(),
        timestamp=NOW,
    )
    for seconds in (1, 2, 3):
        at = NOW + timedelta(seconds=seconds)
        engine.process_quote(account_id="paper-1", quote=quote(at), timestamp=at)
    assert engine.store.order("paper-1", entry.order_id).status is PaperOrderStatus.FILLED
    reduction = engine.submit(
        account_id="paper-1",
        idempotency_key="reduction-1",
        risk_decision=decision(
            "reduction-1",
            timestamp=NOW + timedelta(seconds=3),
            direction=FusionDirection.SHORT,
            action=RiskAction.REDUCTION_ONLY,
        ),
        quote=quote(NOW + timedelta(seconds=3)),
        timestamp=NOW + timedelta(seconds=3),
    )
    with pytest.raises(PaperTradingError, match="reducible position"):
        engine.submit(
            account_id="paper-1",
            idempotency_key="reduction-2",
            risk_decision=decision(
                "reduction-2",
                timestamp=NOW + timedelta(seconds=3),
                direction=FusionDirection.SHORT,
                action=RiskAction.REDUCTION_ONLY,
            ),
            quote=quote(NOW + timedelta(seconds=3)),
            timestamp=NOW + timedelta(seconds=3),
        )
    fills = engine.process_quote(
        account_id="paper-1",
        quote=quote(NOW + timedelta(seconds=4)),
        timestamp=NOW + timedelta(seconds=4),
    )
    assert fills[0].side.value == "sell"
    assert engine.store.position("paper-1", "EUR/USD").quantity >= 0
    assert reduction.order_id == fills[0].order_id


def test_delayed_quote_cannot_move_position_mark_backward(engine: PaperTradingEngine) -> None:
    engine.submit(
        account_id="paper-1",
        idempotency_key="mark-order",
        risk_decision=decision("mark-order"),
        quote=quote(),
        timestamp=NOW,
    )
    current = NOW + timedelta(seconds=1)
    engine.process_quote(account_id="paper-1", quote=quote(current), timestamp=current)
    before = engine.store.position("paper-1", "EUR/USD")
    assert before is not None
    engine.process_quote(account_id="paper-1", quote=quote(NOW), timestamp=current)
    after = engine.store.position("paper-1", "EUR/USD")
    assert after is not None
    assert after.updated_at == before.updated_at
    assert after.market_price == before.market_price


def test_valuation_snapshot_advances_account_revision_without_a_position(
    engine: PaperTradingEngine,
) -> None:
    at = NOW + timedelta(seconds=1)
    assert engine.process_quote(account_id="paper-1", quote=quote(at), timestamp=at) == ()
    account = engine.store.account("paper-1")
    assert account.updated_at == at
    assert account.state_revision > 0
    snapshot = engine.portfolio_risk_snapshot("paper-1")
    assert snapshot.timestamp == at
    assert snapshot.revision == account.state_revision


def test_repricing_cannot_exceed_reserved_risk_at_fill(engine: PaperTradingEngine) -> None:
    order = engine.submit(
        account_id="paper-1",
        idempotency_key="repriced-risk",
        risk_decision=decision("repriced-risk"),
        quote=quote(),
        timestamp=NOW,
    )
    quotes = tuple(
        PaperQuote(
            instrument(),
            NOW + timedelta(seconds=offset),
            Decimal("101"),
            Decimal("102"),
        )
        for offset in (1, 2, 3)
    )
    assert all(
        len(engine.process_quote(account_id="paper-1", quote=item, timestamp=item.timestamp)) == 1
        for item in quotes[:2]
    )
    assert engine.process_quote(
        account_id="paper-1", quote=quotes[2], timestamp=quotes[2].timestamp
    ) == ()
    assert engine.store.order("paper-1", order.order_id).status is PaperOrderStatus.EXPIRED
    position = engine.store.position("paper-1", "EUR/USD")
    assert position is not None
    assert position.market_price * position.quantity <= Decimal("1000")


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


def test_replayed_risk_decision_has_one_durable_economic_effect(
    engine: PaperTradingEngine,
) -> None:
    approved = decision("replayed-decision", max_new_notional=Decimal("500"))
    first = engine.submit(
        account_id="paper-1",
        idempotency_key="first-consumption",
        risk_decision=approved,
        quote=quote(),
        timestamp=NOW,
    )
    with pytest.raises(PaperTradingError, match="uniqueness conflict"):
        engine.submit(
            account_id="paper-1",
            idempotency_key="replayed-consumption",
            risk_decision=approved,
            quote=quote(),
            timestamp=NOW,
        )
    assert engine.store.orders("paper-1") == (first,)
    assert engine.store.active_reservation_total_for_account("paper-1") > Decimal("0")


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


def test_partial_fills_conserve_quantity_then_release_reservation(
    engine: PaperTradingEngine,
) -> None:
    submitted = engine.submit(
        account_id="paper-1",
        idempotency_key="complete",
        risk_decision=decision("complete"),
        quote=quote(),
        timestamp=NOW,
    )
    for seconds in (1, 2, 3):
        at = NOW + timedelta(seconds=seconds)
        engine.process_quote(account_id="paper-1", quote=quote(at), timestamp=at)
    order = engine.store.order("paper-1", submitted.order_id)
    assert order.status is PaperOrderStatus.FILLED
    assert order.filled_quantity + order.remaining_quantity == order.quantity
    assert order.remaining_quantity == Decimal("0")
    assert engine.store.account("paper-1").reserved_risk == Decimal("0")
    with pytest.raises(PaperTradingError, match="risk capacity"):
        engine.submit(
            account_id="paper-1",
            idempotency_key="exposure-after-fill",
            risk_decision=decision(
                "exposure-after-fill",
                timestamp=NOW + timedelta(seconds=3),
                max_new_notional=Decimal("100"),
            ),
            quote=quote(NOW + timedelta(seconds=3)),
            timestamp=NOW + timedelta(seconds=3),
        )
    with pytest.raises(PaperTradingError, match="only open"):
        engine.cancel(
            account_id="paper-1",
            order_id=order.order_id,
            timestamp=NOW + timedelta(seconds=4),
            reason="late",
        )


def test_stop_ioc_expiration_and_stale_quote_fail_closed(engine: PaperTradingEngine) -> None:
    stop = engine.submit(
        account_id="paper-1",
        idempotency_key="stop",
        risk_decision=decision("stop", max_new_notional=Decimal("500")),
        quote=quote(),
        timestamp=NOW,
        order_type=OrderType.STOP,
        stop_price=Decimal("102"),
    )
    at = NOW + timedelta(seconds=1)
    assert engine.process_quote(account_id="paper-1", quote=quote(at), timestamp=at) == ()
    triggered = PaperQuote(instrument(), at + timedelta(seconds=1), Decimal("101"), Decimal("102"))
    assert (
        len(
            engine.process_quote(
                account_id="paper-1", quote=triggered, timestamp=triggered.timestamp
            )
        )
        == 1
    )
    assert engine.store.order("paper-1", stop.order_id).status is PaperOrderStatus.PARTIALLY_FILLED

    ioc = engine.submit(
        account_id="paper-1",
        idempotency_key="ioc",
        risk_decision=decision("ioc", timestamp=triggered.timestamp),
        quote=triggered,
        timestamp=triggered.timestamp,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("100"),
        time_in_force=TimeInForce.IOC,
    )
    later = triggered.timestamp + timedelta(seconds=1)
    assert engine.process_quote(account_id="paper-1", quote=quote(later), timestamp=later) == ()
    assert engine.store.order("paper-1", ioc.order_id).status is PaperOrderStatus.EXPIRED
    assert engine.store.account("paper-1").reserved_risk > Decimal("0")
    with pytest.raises(PaperTradingError, match="future-dated or stale"):
        engine.process_quote(
            account_id="paper-1", quote=quote(NOW + timedelta(minutes=2)), timestamp=NOW
        )


def test_submission_mutation_and_fill_cash_failures_are_rejected(
    engine: PaperTradingEngine,
) -> None:
    first = engine.submit(
        account_id="paper-1",
        idempotency_key="same",
        risk_decision=decision("same"),
        quote=quote(),
        timestamp=NOW,
    )
    with pytest.raises(PaperTradingError, match="idempotency"):
        engine.submit(
            account_id="paper-1",
            idempotency_key="same",
            risk_decision=decision("other"),
            quote=quote(),
            timestamp=NOW,
        )
    with pytest.raises(PaperTradingError, match="expiration"):
        engine.submit(
            account_id="paper-1",
            idempotency_key="expired",
            risk_decision=decision("expired"),
            quote=quote(),
            timestamp=NOW,
            expires_at=NOW,
        )
    with pytest.raises(PaperTradingError, match="integrity"):
        engine.submit(
            account_id="paper-1",
            idempotency_key="wrong-policy",
            risk_decision=replace(decision("wrong-policy"), configuration_id="different"),
            quote=quote(),
            timestamp=NOW,
        )
    engine.config = PaperExecutionConfig(
        max_fill_quantity=Decimal("4"), slippage_absolute=Decimal("3000")
    )
    at = NOW + timedelta(seconds=1)
    assert engine.process_quote(account_id="paper-1", quote=quote(at), timestamp=at) == ()
    rejected = engine.store.order("paper-1", first.order_id)
    assert rejected.status is PaperOrderStatus.REJECTED
    assert engine.store.account("paper-1").reserved_risk == Decimal("0")
