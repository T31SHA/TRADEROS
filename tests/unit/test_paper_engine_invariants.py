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
            max_new_notional=Decimal("500"),
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


def test_risk_day_and_loss_locks_are_persisted_from_accounting_state(
    engine: PaperTradingEngine,
) -> None:
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
    with pytest.raises(PaperTradingError, match="risk lock"):
        engine.submit(
            account_id="engine-account",
            idempotency_key="blocked-by-loss-lock",
            risk_decision=_decision("blocked-by-loss-lock"),
            quote=_quote(),
            timestamp=NOW,
        )


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
