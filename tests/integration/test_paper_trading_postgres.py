"""Real PostgreSQL verification for the Phase 8 durable paper boundary.

Run only against a disposable database whose migrations 001 through 004 have been
applied, for example with ``TRADEROS_POSTGRES_TEST_URL`` set by the local test
harness.  No test creates a network or broker connection.
"""

from __future__ import annotations

import inspect
import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from multiprocessing import Barrier, Queue, get_context
from queue import Empty

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from traderos.data.instruments import AssetClass, Instrument
from traderos.database.connection import create_database_engine
from traderos.database.schema import paper_accounts
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
    RiskReasonCode,
    risk_decision_integrity_id,
)
from traderos.signals import FusionDirection

POSTGRES_URL_ENV = "TRADEROS_POSTGRES_TEST_URL"
NOW = datetime(2024, 1, 2, 12, tzinfo=UTC)
ACCOUNT = "phase8-postgres"


def _instrument(symbol: str = "EUR/USD") -> Instrument:
    return Instrument(
        canonical_symbol=symbol,
        asset_class=AssetClass.FOREX,
        base_currency="EUR",
        quote_currency="USD",
        trading_currency="USD",
    )


def _quote(timestamp: datetime = NOW, symbol: str = "EUR/USD") -> PaperQuote:
    return PaperQuote(_instrument(symbol), timestamp, Decimal("100"), Decimal("101"))


def _decision(
    identifier: str,
    *,
    timestamp: datetime = NOW,
    direction: FusionDirection = FusionDirection.LONG,
    action: RiskAction = RiskAction.NEW_OR_INCREASE,
    max_new_notional: Decimal = Decimal("1000"),
) -> RiskDecision:
    value = RiskDecision(
        decision_id=identifier,
        status=RiskDecisionStatus.APPROVE,
        decision_timestamp=timestamp,
        intent_id=f"intent-{identifier}",
        account_id=ACCOUNT,
        instrument=_instrument(),
        direction=direction,
        policy_id="risk_firewall",
        policy_version="1",
        configuration_id="postgres-policy-config",
        reason_codes=(),
        checks=(),
        authorization=RiskAuthorization(
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
        ),
        regime_state_id="state-postgres",
        portfolio_snapshot_timestamp=timestamp,
        market_snapshot_timestamp=timestamp,
    )
    return replace(value, decision_id=risk_decision_integrity_id(value))


def _config(*, max_fill: Decimal | None = None) -> PaperExecutionConfig:
    return PaperExecutionConfig(max_fill_quantity=max_fill)


def _create_account(
    engine: PaperTradingEngine, *, risk_capacity: Decimal = Decimal("1000")
) -> None:
    engine.create_account(
        account_id=ACCOUNT,
        account_currency="USD",
        starting_cash=Decimal("10000"),
        risk_capacity=risk_capacity,
        daily_loss_limit=Decimal("500"),
        max_drawdown=Decimal("1000"),
        risk_policy_id="risk_firewall",
        risk_policy_configuration_id="postgres-policy-config",
        timestamp=NOW,
    )


def _new_engine(url: str, *, max_fill: Decimal | None = None) -> PaperTradingEngine:
    return PaperTradingEngine(
        SqlAlchemyPaperStore(create_database_engine(url)), _config(max_fill=max_fill)
    )


def _submit_worker(
    url: str,
    account_id: str,
    identifier: str,
    idempotency_key: str,
    barrier: Barrier,
    results: Queue[tuple[str, str]],
) -> None:
    """Independent process and independent database connection for a real race."""

    engine = _new_engine(url)
    try:
        barrier.wait(timeout=15)
        order = engine.submit(
            account_id=account_id,
            idempotency_key=idempotency_key,
            risk_decision=_decision(identifier),
            quote=_quote(),
            timestamp=NOW,
        )
        results.put(("ok", order.order_id))
    except Exception as exc:  # the parent asserts the fail-closed outcome
        results.put(("error", type(exc).__name__))
    finally:
        engine.store.engine.dispose()


def _fill_or_cancel_worker(
    url: str,
    account_id: str,
    order_id: str,
    operation: str,
    barrier: Barrier,
    results: Queue[tuple[str, str]],
) -> None:
    engine = _new_engine(url, max_fill=Decimal("4"))
    at = NOW + timedelta(seconds=1)
    try:
        barrier.wait(timeout=15)
        if operation == "fill":
            result = engine.process_quote(account_id=account_id, quote=_quote(at), timestamp=at)
            results.put(("fill", str(len(result))))
        else:
            result = engine.cancel(
                account_id=account_id, order_id=order_id, timestamp=at, reason="race-cancel"
            )
            results.put(("cancel", result.status.value))
    except Exception as exc:
        results.put(("error", type(exc).__name__))
    finally:
        engine.store.engine.dispose()


@pytest.fixture
def postgres_url() -> str:
    url = os.environ.get(POSTGRES_URL_ENV)
    if not url:
        pytest.skip(f"{POSTGRES_URL_ENV} is required for real PostgreSQL verification")
    return url


@pytest.fixture
def postgres_engine(postgres_url: str) -> Iterator[PaperTradingEngine]:
    engine = _new_engine(postgres_url)
    with engine.store.engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE paper_audit_events, paper_risk_snapshots, paper_fills, "
                "paper_reservations, paper_orders, paper_positions, paper_risk_locks, "
                "paper_accounts, instruments CASCADE"
            )
        )
    yield engine
    engine.store.engine.dispose()


def _collect(results: Queue[tuple[str, str]], count: int) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    for _ in range(count):
        try:
            values.append(results.get(timeout=20))
        except Empty as exc:
            raise AssertionError("concurrent worker did not report") from exc
    return values


@pytest.mark.integration
def test_postgres_migration_constraints_and_atomic_rollback(
    postgres_engine: PaperTradingEngine,
) -> None:
    _create_account(postgres_engine)
    with postgres_engine.store.engine.connect() as connection:
        relation_names = (
            connection.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename LIKE 'paper_%'"
                )
            )
            .scalars()
            .all()
        )
        assert set(relation_names) == {
            "paper_accounts",
            "paper_positions",
            "paper_orders",
            "paper_reservations",
            "paper_fills",
            "paper_risk_locks",
            "paper_risk_snapshots",
            "paper_audit_events",
        }
        constraints = connection.execute(
            text(
                "SELECT contype, conname FROM pg_constraint "
                "WHERE conrelid = 'paper_orders'::regclass"
            )
        ).all()
        assert {"p", "u", "f", "c"} <= {item[0] for item in constraints}
        assert {
            "ck_paper_order_values",
            "ck_paper_order_status",
            "ck_paper_order_prices",
        } <= {item[1] for item in constraints}
        assert (
            connection.execute(
                text("SELECT to_regclass('public.ix_paper_orders_open')")
            ).scalar_one()
            == "ix_paper_orders_open"
        )

    with pytest.raises(IntegrityError), postgres_engine.store.engine.begin() as connection:
        connection.execute(
            paper_accounts.insert().values(
                account_id="invalid",
                account_currency="USD",
                starting_cash=Decimal("0"),
                cash=Decimal("0"),
                reserved_risk=Decimal("0"),
                risk_capacity=Decimal("1"),
                daily_loss_limit=Decimal("1"),
                max_drawdown=Decimal("1"),
                risk_policy_id="risk_firewall",
                risk_policy_configuration_id="postgres-policy-config",
                risk_day="2024-01-02",
                risk_day_starting_equity=Decimal("1"),
                high_water_mark=Decimal("1"),
                realized_pnl=Decimal("0"),
                fees=Decimal("0"),
                created_at=NOW,
                updated_at=NOW,
            )
        )

    with postgres_engine.store.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE OR REPLACE FUNCTION phase8_fail_audit() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN IF NEW.event_type = 'order_created' "
                "THEN RAISE EXCEPTION 'test failure'; END IF; "
                "RETURN NEW; END; $$"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER phase8_fail_audit_trigger BEFORE INSERT ON paper_audit_events "
                "FOR EACH ROW EXECUTE FUNCTION phase8_fail_audit()"
            )
        )
    with pytest.raises(PaperTradingError, match="submission transaction failed"):
        postgres_engine.submit(
            account_id=ACCOUNT,
            idempotency_key="rollback",
            risk_decision=_decision("rollback-decision"),
            quote=_quote(),
            timestamp=NOW,
        )
    with postgres_engine.store.engine.begin() as connection:
        connection.execute(text("DROP TRIGGER phase8_fail_audit_trigger ON paper_audit_events"))
        connection.execute(text("DROP FUNCTION phase8_fail_audit()"))
    assert postgres_engine.store.orders(ACCOUNT) == ()
    assert postgres_engine.store.active_reservation_total_for_account(ACCOUNT) == Decimal("0")


@pytest.mark.integration
def test_postgres_multiprocess_reservation_and_exposure_races(
    postgres_url: str, postgres_engine: PaperTradingEngine
) -> None:
    _create_account(postgres_engine)
    context = get_context("spawn")
    barrier = context.Barrier(2)
    results: Queue[tuple[str, str]] = context.Queue()
    workers = [
        context.Process(
            target=_submit_worker,
            args=(postgres_url, ACCOUNT, f"race-{item}", f"request-{item}", barrier, results),
        )
        for item in range(2)
    ]
    for worker in workers:
        worker.start()
    outcome = _collect(results, 2)
    for worker in workers:
        worker.join(timeout=20)
        assert worker.exitcode == 0
    assert sum(item[0] == "ok" for item in outcome) == 1
    assert len(postgres_engine.store.orders(ACCOUNT)) == 1
    reservation_total = postgres_engine.store.active_reservation_total_for_account(ACCOUNT)
    assert Decimal("0") < reservation_total <= Decimal("1000")
    with postgres_engine.store.engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM paper_reservations WHERE account_id = :account"),
                {"account": ACCOUNT},
            ).scalar_one()
            == 1
        )


@pytest.mark.integration
def test_postgres_multiprocess_idempotency_race(
    postgres_url: str, postgres_engine: PaperTradingEngine
) -> None:
    _create_account(postgres_engine)
    context = get_context("spawn")
    barrier = context.Barrier(2)
    results: Queue[tuple[str, str]] = context.Queue()
    workers = [
        context.Process(
            target=_submit_worker,
            args=(postgres_url, ACCOUNT, "same-decision", "same-request", barrier, results),
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    outcome = _collect(results, 2)
    for worker in workers:
        worker.join(timeout=20)
        assert worker.exitcode == 0
    assert {item[1] for item in outcome if item[0] == "ok"} == {
        postgres_engine.store.orders(ACCOUNT)[0].order_id
    }
    assert len(postgres_engine.store.orders(ACCOUNT)) == 1
    assert (
        Decimal("0")
        < postgres_engine.store.active_reservation_total_for_account(ACCOUNT)
        <= Decimal("1000")
    )
    with postgres_engine.store.engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM paper_reservations WHERE account_id = :account"),
                {"account": ACCOUNT},
            ).scalar_one()
            == 1
        )


@pytest.mark.integration
def test_postgres_fill_cancel_race_has_one_coherent_economic_effect(
    postgres_url: str, postgres_engine: PaperTradingEngine
) -> None:
    _create_account(postgres_engine)
    submitted = postgres_engine.submit(
        account_id=ACCOUNT,
        idempotency_key="fill-cancel",
        risk_decision=_decision("fill-cancel-decision"),
        quote=_quote(),
        timestamp=NOW,
    )
    context = get_context("spawn")
    barrier = context.Barrier(2)
    results: Queue[tuple[str, str]] = context.Queue()
    workers = [
        context.Process(
            target=_fill_or_cancel_worker,
            args=(postgres_url, ACCOUNT, submitted.order_id, operation, barrier, results),
        )
        for operation in ("fill", "cancel")
    ]
    for worker in workers:
        worker.start()
    _collect(results, 2)
    for worker in workers:
        worker.join(timeout=20)
        assert worker.exitcode == 0
    order = postgres_engine.store.order(ACCOUNT, submitted.order_id)
    fills = postgres_engine.store.fills(ACCOUNT)
    assert sum((item.quantity for item in fills), Decimal("0")) == order.filled_quantity
    assert Decimal("0") <= order.filled_quantity <= order.quantity
    assert not (
        order.status in {PaperOrderStatus.CANCELLED, PaperOrderStatus.REJECTED}
        and order.filled_quantity > 0
        and not fills
    )
    postgres_engine.reconcile(ACCOUNT)


@pytest.mark.integration
def test_postgres_recovery_replay_and_authorization_guards(
    postgres_engine: PaperTradingEngine,
) -> None:
    _create_account(postgres_engine)
    approved = _decision("recover-decision")
    submitted = postgres_engine.submit(
        account_id=ACCOUNT,
        idempotency_key="crash-before-ack",
        risk_decision=approved,
        quote=_quote(),
        timestamp=NOW,
    )
    restarted = _new_engine(str(postgres_engine.store.engine.url), max_fill=Decimal("4"))
    retried = restarted.submit(
        account_id=ACCOUNT,
        idempotency_key="crash-before-ack",
        risk_decision=approved,
        quote=_quote(),
        timestamp=NOW,
    )
    assert retried.order_id == submitted.order_id
    first_event = _quote(NOW + timedelta(seconds=1))
    assert (
        len(
            restarted.process_quote(
                account_id=ACCOUNT, quote=first_event, timestamp=first_event.timestamp
            )
        )
        == 1
    )
    recovered, positions, orders = restarted.recover(ACCOUNT)
    assert orders[0].status is PaperOrderStatus.PARTIALLY_FILLED
    assert orders[0].filled_quantity == Decimal("4")
    assert orders[0].remaining_quantity == orders[0].quantity - Decimal("4")
    assert positions[0].quantity == Decimal("4")
    cash_after_partial = recovered.cash
    assert (
        restarted.process_quote(
            account_id=ACCOUNT, quote=first_event, timestamp=first_event.timestamp
        )
        == ()
    )
    assert restarted.recover(ACCOUNT)[0].cash == cash_after_partial

    restarted.activate_risk_lock(
        account_id=ACCOUNT,
        lock_type=PaperRiskLockType.EMERGENCY_LOCK,
        reason="recovery-test",
        timestamp=NOW + timedelta(seconds=2),
    )
    second_restart = _new_engine(str(postgres_engine.store.engine.url))
    assert PaperRiskLockType.EMERGENCY_LOCK in second_restart.store.active_locks(ACCOUNT)
    with pytest.raises(PaperTradingError, match="risk lock"):
        second_restart.submit(
            account_id=ACCOUNT,
            idempotency_key="blocked-after-restart",
            risk_decision=_decision("blocked-decision", timestamp=NOW + timedelta(seconds=2)),
            quote=_quote(NOW + timedelta(seconds=2)),
            timestamp=NOW + timedelta(seconds=2),
        )
    stale = _decision("stale", timestamp=NOW)
    with pytest.raises(PaperTradingError, match="stale"):
        second_restart.submit(
            account_id=ACCOUNT,
            idempotency_key="stale",
            risk_decision=stale,
            quote=_quote(NOW + timedelta(minutes=6)),
            timestamp=NOW + timedelta(minutes=6),
        )
    with pytest.raises(PaperTradingError, match="instrument"):
        second_restart.submit(
            account_id=ACCOUNT,
            idempotency_key="wrong-instrument",
            risk_decision=_decision("wrong-instrument"),
            quote=_quote(symbol="USD/JPY"),
            timestamp=NOW,
        )
    for label, tampered in (
        ("direction", replace(_decision("tamper-direction"), direction=FusionDirection.SHORT)),
        ("instrument", replace(_decision("tamper-instrument"), instrument=_instrument("USD/JPY"))),
        (
            "authorization",
            replace(
                _decision("tamper-authorization"),
                authorization=RiskAuthorization(
                    action=RiskAction.NEW_OR_INCREASE,
                    max_new_notional=Decimal("2000"),
                    max_loss_at_stop=Decimal("100"),
                    max_reduction_notional=Decimal("0"),
                ),
            ),
        ),
    ):
        with pytest.raises(PaperTradingError, match="integrity"):
            second_restart.submit(
                account_id=ACCOUNT,
                idempotency_key=f"tamper-{label}",
                risk_decision=tampered,
                quote=_quote(),
                timestamp=NOW,
            )
    restarted.store.engine.dispose()
    second_restart.store.engine.dispose()


@pytest.mark.integration
def test_postgres_store_rejects_invalid_durable_transition(
    postgres_engine: PaperTradingEngine,
) -> None:
    _create_account(postgres_engine)
    order = postgres_engine.submit(
        account_id=ACCOUNT,
        idempotency_key="state-machine",
        risk_decision=_decision("state-machine"),
        quote=_quote(),
        timestamp=NOW,
    )
    invalid = replace(order, status=PaperOrderStatus.CREATED)
    with (
        pytest.raises(PaperTradingError, match="invalid durable order transition"),
        postgres_engine.store.engine.begin() as connection,
    ):
        postgres_engine.store.update_order(connection, invalid, NOW + timedelta(seconds=1))


@pytest.mark.integration
def test_rejected_phase7_decision_cannot_submit(postgres_engine: PaperTradingEngine) -> None:
    _create_account(postgres_engine)
    rejected = replace(
        _decision("rejected"),
        status=RiskDecisionStatus.REJECT,
        authorization=None,
        reason_codes=(RiskReasonCode.KILL_SWITCH_ACTIVE,),
    )
    rejected = replace(rejected, decision_id=risk_decision_integrity_id(rejected))
    with pytest.raises(PaperTradingError, match="approved risk decision"):
        postgres_engine.submit(
            account_id=ACCOUNT,
            idempotency_key="rejected",
            risk_decision=rejected,
            quote=_quote(),
            timestamp=NOW,
        )
    assert postgres_engine.store.orders(ACCOUNT) == ()


def test_public_engine_has_no_direct_intent_or_fill_mutator() -> None:
    assert not hasattr(PaperTradingEngine, "create_order")
    assert not hasattr(PaperTradingEngine, "apply_fill")
    assert "UnifiedTradeIntent" not in inspect.getsource(PaperTradingEngine)
