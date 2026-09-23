"""Failure and accounting invariants for Phase 8 execution orchestration."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from traderos.backtesting.models import OrderSide, OrderType
from traderos.data.instruments import AssetClass, Instrument
from traderos.database.connection import create_database_engine
from traderos.paper import (
    PaperExecutionConfig,
    PaperFill,
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
    RiskLock,
    RiskReasonCode,
    risk_decision_integrity_id,
)
from traderos.signals import FusionDirection

NOW = datetime(2024, 1, 2, 12, tzinfo=UTC)


def _instrument() -> Instrument:
    return Instrument(
        canonical_symbol="EUR/USD",
        asset_class=AssetClass.FOREX,
        base_currency="EUR",
        quote_currency="USD",
        trading_currency="USD",
    )


def _quote(timestamp: datetime = NOW) -> PaperQuote:
    return PaperQuote(_instrument(), timestamp, Decimal("100"), Decimal("101"))


def _decision(
    name: str = "engine",
    *,
    timestamp: datetime = NOW,
    direction: FusionDirection = FusionDirection.LONG,
    account_id: str = "engine-account",
    max_new_notional: Decimal = Decimal("500"),
) -> RiskDecision:
    value = RiskDecision(
        decision_id="pending",
        status=RiskDecisionStatus.APPROVE,
        decision_timestamp=timestamp,
        intent_id=f"intent-{name}",
        account_id=account_id,
        instrument=_instrument(),
        direction=direction,
        policy_id="risk_firewall",
        policy_version="1",
        configuration_id="engine-config",
        reason_codes=(),
        checks=(),
        authorization=RiskAuthorization(
            action=RiskAction.NEW_OR_INCREASE,
            max_new_notional=max_new_notional,
            max_loss_at_stop=Decimal("50"),
            max_reduction_notional=Decimal("0"),
        ),
        regime_state_id="state",
        portfolio_snapshot_timestamp=timestamp,
        market_snapshot_timestamp=timestamp,
    )
    return replace(value, decision_id=risk_decision_integrity_id(value))


@pytest.fixture
def engine() -> PaperTradingEngine:
    store = SqlAlchemyPaperStore(create_database_engine("sqlite+pysqlite:///:memory:"))
    store.create_schema_for_testing()
    result = PaperTradingEngine(store, PaperExecutionConfig(max_fill_quantity=Decimal("2")))
    result.create_account(
        account_id="engine-account",
        account_currency="USD",
        starting_cash=Decimal("1000"),
        risk_capacity=Decimal("500"),
        daily_loss_limit=Decimal("10"),
        max_drawdown=Decimal("10"),
        risk_policy_id="risk_firewall",
        risk_policy_configuration_id="engine-config",
        timestamp=NOW,
    )
    yield result
    store.engine.dispose()


def test_decision_validation_rejects_temporal_scope_status_and_tampering(
    engine: PaperTradingEngine,
) -> None:
    valid = _decision()
    for changed, expected in (
        (replace(valid, decision_id="tampered"), "integrity"),
        (replace(valid, decision_timestamp=NOW + timedelta(seconds=1)), "integrity"),
        (replace(valid, direction=FusionDirection.SHORT), "integrity"),
        (replace(valid, account_id="other-account"), "integrity"),
    ):
        with pytest.raises(PaperTradingError, match=expected):
            engine._validate_decision(changed, _quote(), NOW, "engine-account")
    stale = _decision("stale")
    with pytest.raises(PaperTradingError, match="stale"):
        engine._validate_decision(
            stale, _quote(NOW + timedelta(minutes=6)), NOW + timedelta(minutes=6), "engine-account"
        )
    with pytest.raises(PaperTradingError, match="future"):
        engine._validate_decision(
            _decision("future", timestamp=NOW + timedelta(seconds=1)),
            _quote(),
            NOW,
            "engine-account",
        )
    stale_portfolio = _decision("stale-portfolio", timestamp=NOW + timedelta(minutes=6))
    stale_portfolio = replace(
        stale_portfolio,
        portfolio_snapshot_timestamp=NOW,
    )
    stale_portfolio = replace(
        stale_portfolio,
        decision_id=risk_decision_integrity_id(stale_portfolio),
    )
    with pytest.raises(PaperTradingError, match="portfolio snapshot is stale"):
        engine._validate_decision(
            stale_portfolio,
            _quote(NOW + timedelta(minutes=6)),
            NOW + timedelta(minutes=6),
            "engine-account",
        )
    missing_market = replace(_decision("missing-market"), market_snapshot_timestamp=None)
    missing_market = replace(
        missing_market, decision_id=risk_decision_integrity_id(missing_market)
    )
    with pytest.raises(PaperTradingError, match="lacks market snapshot"):
        engine._validate_decision(missing_market, _quote(), NOW, "engine-account")
    future_market = replace(
        _decision("future-market"), market_snapshot_timestamp=NOW + timedelta(seconds=1)
    )
    future_market = replace(future_market, decision_id=risk_decision_integrity_id(future_market))
    with pytest.raises(PaperTradingError, match="market snapshot is future"):
        engine._validate_decision(future_market, _quote(), NOW, "engine-account")
    rejected = replace(
        valid,
        status=RiskDecisionStatus.REJECT,
        authorization=None,
        reason_codes=(RiskReasonCode.KILL_SWITCH_ACTIVE,),
    )
    rejected = replace(rejected, decision_id=risk_decision_integrity_id(rejected))
    with pytest.raises(PaperTradingError, match="approved"):
        engine._validate_decision(rejected, _quote(), NOW, "engine-account")
    wrong_account = replace(valid, account_id="other-account")
    wrong_account = replace(wrong_account, decision_id=risk_decision_integrity_id(wrong_account))
    with pytest.raises(PaperTradingError, match="account"):
        engine._validate_decision(wrong_account, _quote(), NOW, "engine-account")
    with pytest.raises(PaperTradingError, match="account"):
        engine.submit(
            account_id="engine-account",
            idempotency_key="wrong-account",
            risk_decision=_decision("wrong-account", account_id="other-account"),
            quote=_quote(),
            timestamp=NOW,
        )


def test_execution_candidates_and_position_accounting_cover_long_short_reduce_and_reverse(
    engine: PaperTradingEngine,
) -> None:
    market = engine.submit(
        account_id="engine-account",
        idempotency_key="market",
        risk_decision=_decision(),
        quote=_quote(),
        timestamp=NOW,
    )
    assert engine._execution_price(market, _quote()) == (Decimal("101"), Decimal("101"))
    limit = replace(market, order_type=OrderType.LIMIT, limit_price=Decimal("100"))
    assert engine._execution_price(limit, _quote()) is None
    touched = PaperQuote(_instrument(), NOW, Decimal("99"), Decimal("100"))
    assert engine._execution_price(limit, touched) == (Decimal("100"), Decimal("100"))
    stop = replace(market, order_type=OrderType.STOP, stop_price=Decimal("102"))
    assert engine._execution_price(stop, _quote()) is None
    assert engine._execution_price(
        stop, PaperQuote(_instrument(), NOW, Decimal("101"), Decimal("102"))
    ) == (
        Decimal("102"),
        Decimal("102"),
    )

    buy = PaperFill(
        "fill-buy",
        "order",
        "engine-account",
        _instrument(),
        OrderSide.BUY,
        Decimal("2"),
        Decimal("100"),
        Decimal("100"),
        Decimal("1"),
        NOW,
    )
    position, cash, realized = engine._apply_fill(None, buy)
    assert (position.quantity, cash, realized) == (Decimal("2"), Decimal("-201"), Decimal("0"))
    sell = PaperFill(
        "fill-sell",
        "order",
        "engine-account",
        _instrument(),
        OrderSide.SELL,
        Decimal("3"),
        Decimal("110"),
        Decimal("110"),
        Decimal("0"),
        NOW,
    )
    reversed_position, cash, realized = engine._apply_fill(position, sell)
    assert reversed_position.quantity == Decimal("-1")
    assert reversed_position.average_entry_price == Decimal("110")
    assert realized == Decimal("20")
    assert cash == Decimal("330")


def test_expiration_cancel_reconciliation_and_durable_locks(engine: PaperTradingEngine) -> None:
    submitted = engine.submit(
        account_id="engine-account",
        idempotency_key="expiry",
        risk_decision=_decision(),
        quote=_quote(),
        timestamp=NOW,
        expires_at=NOW + timedelta(seconds=1),
    )
    event_at = NOW + timedelta(seconds=1)
    assert (
        engine.process_quote(
            account_id="engine-account", quote=_quote(event_at), timestamp=event_at
        )
        == ()
    )
    assert (
        engine.store.order("engine-account", submitted.order_id).status is PaperOrderStatus.EXPIRED
    )
    with pytest.raises(PaperTradingError, match="cancellation reason"):
        engine.cancel(
            account_id="engine-account", order_id=submitted.order_id, timestamp=event_at, reason=" "
        )
    with pytest.raises(PaperTradingError, match="risk lock reason"):
        engine.activate_risk_lock(
            account_id="engine-account",
            lock_type=PaperRiskLockType.EMERGENCY_LOCK,
            reason=" ",
            timestamp=event_at,
        )
    engine.activate_risk_lock(
        account_id="engine-account",
        lock_type=PaperRiskLockType.EMERGENCY_LOCK,
        reason="test",
        timestamp=event_at,
    )
    engine.activate_risk_lock(
        account_id="engine-account",
        lock_type=PaperRiskLockType.EMERGENCY_LOCK,
        reason="same",
        timestamp=event_at,
    )
    assert engine.store.active_locks("engine-account") == {PaperRiskLockType.EMERGENCY_LOCK}
    engine.reconcile("engine-account")
    with engine.store.engine.begin() as connection:
        connection.execute(
            text("UPDATE paper_accounts SET reserved_risk = 1 WHERE account_id = 'engine-account'")
        )
    with pytest.raises(PaperTradingError, match="reservation ledger"):
        engine.reconcile("engine-account")


def test_store_read_paths_and_transition_guards_are_database_authoritative(
    engine: PaperTradingEngine,
) -> None:
    with pytest.raises(PaperTradingError, match="already exists"):
        engine.create_account(
            account_id="engine-account",
            account_currency="USD",
            starting_cash=Decimal("1"),
            risk_capacity=Decimal("1"),
            daily_loss_limit=Decimal("1"),
            max_drawdown=Decimal("1"),
            risk_policy_id="risk_firewall",
            risk_policy_configuration_id="engine-config",
            timestamp=NOW,
        )
    assert engine.store.position("engine-account", "missing") is None
    assert engine.store.order_by_idempotency("engine-account", "missing") is None
    with pytest.raises(PaperTradingError, match="unknown paper account"):
        engine.store.account("missing")
    with pytest.raises(PaperTradingError, match="unknown paper order"):
        engine.store.order("engine-account", "missing")
    with engine.store.engine.begin() as connection:
        with pytest.raises(PaperTradingError, match="unknown paper account"):
            engine.store.locked_account(connection, "missing")
        with pytest.raises(PaperTradingError, match="unknown paper order"):
            engine.store.locked_order(connection, "engine-account", "missing")

    order = engine.submit(
        account_id="engine-account",
        idempotency_key="guard",
        risk_decision=_decision("guard"),
        quote=_quote(),
        timestamp=NOW,
    )
    with (
        engine.store.engine.begin() as connection,
        pytest.raises(PaperTradingError, match="immutable"),
    ):
        engine.store.update_order(
            connection,
            replace(
                order,
                quantity=Decimal("1"),
                status=PaperOrderStatus.PARTIALLY_FILLED,
                filled_quantity=Decimal("1"),
            ),
            NOW,
        )

    for changed in (
        replace(
            order,
            status=PaperOrderStatus.CANCEL_REQUESTED,
            authorization_action=RiskAction.REDUCTION_ONLY,
        ),
        replace(
            order,
            status=PaperOrderStatus.CANCEL_REQUESTED,
            risk_policy_id="tampered-policy",
        ),
        replace(
            order,
            status=PaperOrderStatus.CANCEL_REQUESTED,
            sizing_configuration_id="tampered-sizing",
        ),
        replace(
            order,
            status=PaperOrderStatus.CANCEL_REQUESTED,
            idempotency_key="tampered-idempotency",
        ),
    ):
        with (
            engine.store.engine.begin() as connection,
            pytest.raises(PaperTradingError, match="immutable"),
        ):
            engine.store.update_order(connection, changed, NOW)

    with (
        engine.store.engine.begin() as connection,
        pytest.raises(PaperTradingError, match="newly filled"),
    ):
        engine.store.update_order(
            connection, replace(order, status=PaperOrderStatus.PARTIALLY_FILLED), NOW
        )
    with engine.store.engine.begin() as connection:
        with pytest.raises(PaperTradingError, match="no remaining"):
            engine.store.update_order(
                connection,
                replace(order, status=PaperOrderStatus.FILLED, filled_quantity=Decimal("1")),
                NOW,
            )
        engine.store.release_reservation(connection, order, NOW)
        engine.store.release_reservation(connection, order, NOW)
    assert engine.store.active_reservation_total_for_account("engine-account") == Decimal("0")
    assert engine.store.orders("engine-account") == (order,)
    events = engine.store.audit_events("engine-account")
    assert events[0]["event_type"] == "account_created"


def test_reconciliation_rejects_open_order_without_matching_active_reservation(
    engine: PaperTradingEngine,
) -> None:
    order = engine.submit(
        account_id="engine-account",
        idempotency_key="reconcile-reservation",
        risk_decision=_decision("reconcile-reservation"),
        quote=_quote(),
        timestamp=NOW,
    )
    with engine.store.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE paper_reservations SET active = 0, released_at = :released "
                "WHERE order_id = :order_id"
            ),
            {"released": NOW + timedelta(seconds=1), "order_id": order.order_id},
        )
    with pytest.raises(PaperTradingError, match="active reservation ledger"):
        engine.reconcile("engine-account")


def test_reconciliation_rejects_reservation_authorization_mismatch(
    engine: PaperTradingEngine,
) -> None:
    order = engine.submit(
        account_id="engine-account",
        idempotency_key="reconcile-authorization",
        risk_decision=_decision("reconcile-authorization"),
        quote=_quote(),
        timestamp=NOW,
    )
    with engine.store.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE paper_reservations SET intent_id = :intent_id "
                "WHERE order_id = :order_id"
            ),
            {"intent_id": "tampered-intent", "order_id": order.order_id},
        )

    with pytest.raises(PaperTradingError, match="reservation authorization"):
        engine.reconcile("engine-account")


def test_reconciliation_rejects_tampered_risk_snapshot_projection(
    engine: PaperTradingEngine,
) -> None:
    snapshot = engine.risk_snapshot("engine-account")
    with engine.store.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE paper_risk_snapshots SET cash = :cash "
                "WHERE snapshot_id = :snapshot_id"
            ),
            {"cash": "999", "snapshot_id": snapshot.snapshot_id},
        )

    with pytest.raises(PaperTradingError, match="risk snapshot differs"):
        engine.reconcile("engine-account")


def test_risk_day_and_loss_locks_are_persisted_from_accounting_state(
    engine: PaperTradingEngine,
) -> None:
    pending = engine.submit(
        account_id="engine-account",
        idempotency_key="pending-before-loss-lock",
        risk_decision=_decision("pending-before-loss-lock"),
        quote=_quote(),
        timestamp=NOW,
    )
    account = engine.store.account("engine-account")
    with engine.store.engine.begin() as connection:
        rolled = engine._roll_risk_day_if_needed(
            connection, replace(account, risk_day=NOW.date() - timedelta(days=1)), NOW
        )
        assert rolled.risk_day == NOW.date()
        engine._apply_loss_locks(connection, rolled, Decimal("980"), NOW)
    locks = engine.store.active_locks("engine-account")
    assert PaperRiskLockType.DAILY_LOSS_LOCK in locks
    assert PaperRiskLockType.DRAWDOWN_LOCK in locks
    assert (
        engine.store.order("engine-account", pending.order_id).status
        is PaperOrderStatus.CANCELLED
    )
    assert engine.store.active_reservation_total_for_account("engine-account") == Decimal("0")
    with pytest.raises(PaperTradingError, match="risk lock"):
        engine.submit(
            account_id="engine-account",
            idempotency_key="blocked-by-loss-lock",
            risk_decision=_decision("blocked-by-loss-lock"),
            quote=_quote(),
            timestamp=NOW,
        )


def test_durable_risk_snapshots_capture_reservations_marks_and_loss_locks(
    engine: PaperTradingEngine,
) -> None:
    initial = engine.risk_snapshot("engine-account")
    assert initial.equity == Decimal("1000")
    assert initial.pending_order_count == 0
    assert initial.mark_timestamp is None

    submitted = engine.submit(
        account_id="engine-account",
        idempotency_key="snapshots",
        risk_decision=_decision("snapshots"),
        quote=_quote(),
        timestamp=NOW,
    )
    reserved = engine.risk_snapshot("engine-account")
    assert reserved.pending_order_count == 1
    assert reserved.reserved_risk > 0

    for second in (1, 2, 3):
        event_at = NOW + timedelta(seconds=second)
        engine.process_quote(
            account_id="engine-account", quote=_quote(event_at), timestamp=event_at
        )
    assert (
        engine.store.order("engine-account", submitted.order_id).status is PaperOrderStatus.FILLED
    )

    loss_at = NOW + timedelta(seconds=4)
    engine.process_quote(
        account_id="engine-account",
        quote=PaperQuote(_instrument(), loss_at, Decimal("1"), Decimal("2")),
        timestamp=loss_at,
    )
    loss_snapshot = engine.risk_snapshot("engine-account")
    assert loss_snapshot.mark_timestamp == loss_at
    assert loss_snapshot.equity < Decimal("1000")
    assert PaperRiskLockType.DAILY_LOSS_LOCK in loss_snapshot.active_locks
    assert PaperRiskLockType.DRAWDOWN_LOCK in loss_snapshot.active_locks
    assert len(engine.store.risk_snapshots("engine-account")) >= 6
    firewall_snapshot = engine.portfolio_risk_snapshot("engine-account")
    assert firewall_snapshot.equity == loss_snapshot.equity
    assert firewall_snapshot.pending_intent_count == loss_snapshot.pending_order_count
    assert {RiskLock.DRAWDOWN, RiskLock.MANUAL} <= firewall_snapshot.active_locks
    engine.reconcile("engine-account")


def test_submission_rejects_a_superseded_portfolio_snapshot_and_validates_order_shape(
    engine: PaperTradingEngine,
) -> None:
    submitted = engine.submit(
        account_id="engine-account",
        idempotency_key="snapshot-current",
        risk_decision=_decision("snapshot-current"),
        quote=_quote(),
        timestamp=NOW,
    )
    advanced = NOW + timedelta(seconds=1)
    engine.process_quote(account_id="engine-account", quote=_quote(advanced), timestamp=advanced)
    stale = _decision("superseded", timestamp=advanced)
    stale = replace(stale, portfolio_snapshot_timestamp=NOW)
    stale = replace(stale, decision_id=risk_decision_integrity_id(stale))
    with pytest.raises(PaperTradingError, match="no longer current"):
        engine.submit(
            account_id="engine-account",
            idempotency_key="superseded",
            risk_decision=stale,
            quote=_quote(advanced),
            timestamp=advanced,
        )
    with pytest.raises(PaperTradingError, match="limit order requires only"):
        engine._validate_order_request(OrderType.LIMIT, None, Decimal("1"))
    with pytest.raises(PaperTradingError, match="limit order price"):
        engine._validate_order_request(OrderType.LIMIT, Decimal("0"), None)
    with pytest.raises(PaperTradingError, match="stop order requires only"):
        engine._validate_order_request(OrderType.STOP, Decimal("1"), None)
    with pytest.raises(PaperTradingError, match="stop order price"):
        engine._validate_order_request(OrderType.STOP, None, Decimal("0"))
    assert engine.store.order("engine-account", submitted.order_id).filled_quantity > 0
    cancelled = engine.cancel(
        account_id="engine-account",
        order_id=submitted.order_id,
        timestamp=advanced + timedelta(seconds=1),
        reason="test cancellation",
    )
    assert cancelled.status is PaperOrderStatus.CANCELLED


def test_engine_rejects_invalid_quotes_policy_scope_and_locked_new_risk(
    engine: PaperTradingEngine,
) -> None:
    with pytest.raises(PaperTradingError, match="idempotency"):
        engine.submit(
            account_id="engine-account",
            idempotency_key=" ",
            risk_decision=_decision(),
            quote=_quote(),
            timestamp=NOW,
        )
    with pytest.raises(PaperTradingError, match="policy identity"):
        engine._validate_account_authorization(
            engine.store.account("engine-account"), replace(_decision(), configuration_id="other")
        )
    with pytest.raises(PaperTradingError, match="instrument"):
        engine._validate_decision(
            _decision(),
            PaperQuote(
                Instrument(
                    canonical_symbol="USD/JPY",
                    asset_class=AssetClass.FOREX,
                    quote_currency="USD",
                    trading_currency="USD",
                ),
                NOW,
                Decimal("100"),
                Decimal("101"),
            ),
            NOW,
            "engine-account",
        )
    with pytest.raises(PaperTradingError, match="market order"):
        engine.submit(
            account_id="engine-account",
            idempotency_key="invalid-market-shape",
            risk_decision=_decision("invalid-market-shape"),
            quote=_quote(),
            timestamp=NOW,
            limit_price=Decimal("100"),
        )
    engine.activate_risk_lock(
        account_id="engine-account",
        lock_type=PaperRiskLockType.EMERGENCY_LOCK,
        reason="block",
        timestamp=NOW,
    )
    with pytest.raises(PaperTradingError, match="risk lock"):
        engine.submit(
            account_id="engine-account",
            idempotency_key="blocked",
            risk_decision=_decision("blocked"),
            quote=_quote(),
            timestamp=NOW,
        )


def test_execution_rejects_stale_quote_nonexecutable_direction_and_invalid_slippage(
    engine: PaperTradingEngine,
) -> None:
    at = NOW + timedelta(minutes=6)
    with pytest.raises(PaperTradingError, match="quote is stale"):
        engine.submit(
            account_id="engine-account",
            idempotency_key="stale-quote",
            risk_decision=_decision("stale-quote", timestamp=at),
            quote=_quote(NOW),
            timestamp=at,
        )
    flat = replace(_decision("flat"), direction=FusionDirection.FLAT)
    flat = replace(flat, decision_id=risk_decision_integrity_id(flat))
    with pytest.raises(PaperTradingError, match="not executable"):
        engine._validate_decision(flat, _quote(), NOW, "engine-account")

    short = engine.submit(
        account_id="engine-account",
        idempotency_key="invalid-slippage",
        risk_decision=_decision("invalid-slippage", direction=FusionDirection.SHORT),
        quote=_quote(),
        timestamp=NOW,
    )
    engine.config = PaperExecutionConfig(
        max_fill_quantity=Decimal("2"), slippage_absolute=Decimal("100")
    )
    with pytest.raises(PaperTradingError, match="invalid execution"):
        engine._execution_price(short, _quote())


def test_submission_and_fill_integrity_conflicts_fail_without_projection_mutation(
    engine: PaperTradingEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = _decision("insert-conflict")

    def raise_insert_conflict(*_args: object, **_kwargs: object) -> None:
        raise IntegrityError("insert", {}, Exception("unique"))

    monkeypatch.setattr(engine.store, "insert_order_and_reservation", raise_insert_conflict)
    with pytest.raises(PaperTradingError, match="uniqueness conflict"):
        engine.submit(
            account_id="engine-account",
            idempotency_key="insert-conflict",
            risk_decision=decision,
            quote=_quote(),
            timestamp=NOW,
        )
    assert engine.store.orders("engine-account") == ()
    monkeypatch.undo()

    order = engine.submit(
        account_id="engine-account",
        idempotency_key="fill-conflict",
        risk_decision=_decision("fill-conflict"),
        quote=_quote(),
        timestamp=NOW,
    )
    persisted_order = engine.store.order("engine-account", order.order_id)
    expected_fill_quantity = min(
        persisted_order.remaining_quantity, engine.config.max_fill_quantity or Decimal("0")
    )
    duplicate = PaperFill(
        fill_id=f"{order.order_id}:{persisted_order.filled_quantity + expected_fill_quantity}",
        order_id=order.order_id,
        account_id=order.account_id,
        instrument=order.instrument,
        side=order.side,
        quantity=Decimal("2"),
        price=Decimal("101"),
        reference_price=Decimal("101"),
        commission=Decimal("0"),
        timestamp=NOW + timedelta(seconds=1),
    )
    with engine.store.engine.begin() as connection:
        engine.store.insert_fill(connection, duplicate)
    with pytest.raises(PaperTradingError, match="duplicate or invalid paper fill"):
        engine.process_quote(
            account_id="engine-account",
            quote=_quote(NOW + timedelta(seconds=1)),
            timestamp=NOW + timedelta(seconds=1),
        )
    persisted = engine.store.order("engine-account", order.order_id)
    assert persisted.status is PaperOrderStatus.ACCEPTED
    assert persisted.filled_quantity == Decimal("0")


def test_default_fill_size_and_flat_position_are_exact_decimal_projections(
    engine: PaperTradingEngine,
) -> None:
    engine.config = PaperExecutionConfig()
    order = engine.submit(
        account_id="engine-account",
        idempotency_key="unbounded-fill",
        risk_decision=_decision("unbounded-fill"),
        quote=_quote(),
        timestamp=NOW,
    )
    fills = engine.process_quote(
        account_id="engine-account",
        quote=_quote(NOW + timedelta(seconds=1)),
        timestamp=NOW + timedelta(seconds=1),
    )
    assert fills[0].quantity == order.quantity
    assert engine.store.order("engine-account", order.order_id).status is PaperOrderStatus.FILLED

    opened = PaperFill(
        "open",
        "manual",
        "engine-account",
        _instrument(),
        OrderSide.BUY,
        Decimal("2"),
        Decimal("100"),
        Decimal("100"),
        Decimal("0"),
        NOW,
    )
    position, _, _ = engine._apply_fill(None, opened)
    closing = PaperFill(
        "close",
        "manual",
        "engine-account",
        _instrument(),
        OrderSide.SELL,
        Decimal("2"),
        Decimal("101"),
        Decimal("101"),
        Decimal("0"),
        NOW,
    )
    flat, _, realized = engine._apply_fill(position, closing)
    assert flat.quantity == Decimal("0")
    assert flat.average_entry_price == Decimal("0")
    assert realized == Decimal("2")


def test_original_trade_authorization_bounds_survive_partial_adverse_fills_and_restart(
    engine: PaperTradingEngine,
) -> None:
    account_id = "bounded-account"
    engine.create_account(
        account_id=account_id,
        account_currency="USD",
        starting_cash=Decimal("10000"),
        risk_capacity=Decimal("10000"),
        daily_loss_limit=Decimal("500"),
        max_drawdown=Decimal("1000"),
        risk_policy_id="risk_firewall",
        risk_policy_configuration_id="engine-config",
        timestamp=NOW,
    )
    bounded = PaperTradingEngine(
        engine.store,
        PaperExecutionConfig(
            max_fill_quantity=Decimal("2"),
            commission_rate=Decimal("0.01"),
        ),
    )
    order = bounded.submit(
        account_id=account_id,
        idempotency_key="bounded-trade",
        risk_decision=_decision(
            "bounded-trade",
            account_id=account_id,
            max_new_notional=Decimal("1000"),
        ),
        quote=_quote(),
        timestamp=NOW,
    )
    assert order.authorization_max_new_notional == Decimal("1000")

    first_quote = PaperQuote(
        _instrument(), NOW + timedelta(seconds=1), Decimal("149"), Decimal("150")
    )
    second_quote = PaperQuote(
        _instrument(), NOW + timedelta(seconds=2), Decimal("399"), Decimal("400")
    )
    first_fills = bounded.process_quote(
        account_id=account_id, quote=first_quote, timestamp=first_quote.timestamp
    )
    second_fills = bounded.process_quote(
        account_id=account_id, quote=second_quote, timestamp=second_quote.timestamp
    )

    fills = (*first_fills, *second_fills)
    assert first_fills and second_fills
    total_cost = sum(
        (fill.quantity * fill.price + fill.commission for fill in fills), Decimal("0")
    )
    assert total_cost <= Decimal("1000")
    persisted = engine.store.order(account_id, order.order_id)
    assert persisted.authorization_max_new_notional == Decimal("1000")
    restarted = PaperTradingEngine(
        engine.store,
        PaperExecutionConfig(max_fill_quantity=Decimal("2"), commission_rate=Decimal("0.01")),
    )
    assert restarted.store.order(account_id, order.order_id).authorization_max_new_notional == (
        Decimal("1000")
    )
