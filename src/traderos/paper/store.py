"""SQLAlchemy-backed authoritative state for the offline paper engine."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, and_, select, update
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from traderos.backtesting.models import OrderSide, OrderType, TimeInForce
from traderos.data.instruments import Instrument
from traderos.database.schema import (
    instruments,
    metadata,
    paper_accounts,
    paper_audit_events,
    paper_fills,
    paper_orders,
    paper_positions,
    paper_reservations,
    paper_risk_locks,
    paper_risk_snapshots,
    worker_leases,
)
from traderos.execution.authority import ExecutionAuthority
from traderos.paper.models import (
    PaperAccount,
    PaperFill,
    PaperOrder,
    PaperOrderStatus,
    PaperPosition,
    PaperRiskLockType,
    PaperRiskSnapshot,
    PaperTradingError,
)
from traderos.risk.models import RiskAction


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class SqlAlchemyPaperStore:
    """One transactional state owner; no in-memory lock is relied on for safety."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def create_schema_for_testing(self) -> None:
        """Create all project tables for SQLite-backed integration tests only."""

        metadata.create_all(self.engine)

    def create_account(self, account: PaperAccount) -> PaperAccount:
        row = self._account_row(account)
        try:
            with self.engine.begin() as connection:
                connection.execute(paper_accounts.insert(), row)
                self._audit(connection, account.account_id, "account_created", account.created_at)
                self.record_risk_snapshot(connection, account.account_id, account.created_at)
        except IntegrityError as exc:
            raise PaperTradingError(f"paper account already exists: {account.account_id}") from exc
        except SQLAlchemyError as exc:
            raise PaperTradingError(str(exc)) from exc
        return account

    def account(self, account_id: str) -> PaperAccount:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(paper_accounts).where(paper_accounts.c.account_id == account_id)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise PaperTradingError(f"unknown paper account: {account_id}")
        return self._to_account(row)

    def position(self, account_id: str, symbol: str) -> PaperPosition | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(paper_positions).where(
                        and_(
                            paper_positions.c.account_id == account_id,
                            paper_positions.c.symbol == symbol,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        return self._to_position(row) if row else None

    def positions(self, account_id: str) -> tuple[PaperPosition, ...]:
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(paper_positions)
                    .where(paper_positions.c.account_id == account_id)
                    .order_by(paper_positions.c.symbol)
                )
                .mappings()
                .all()
            )
        return tuple(self._to_position(row) for row in rows)

    def order(self, account_id: str, order_id: str) -> PaperOrder:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(paper_orders).where(
                        and_(
                            paper_orders.c.account_id == account_id,
                            paper_orders.c.order_id == order_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise PaperTradingError(f"unknown paper order: {order_id}")
        return self._to_order(row)

    def order_by_idempotency(self, account_id: str, key: str) -> PaperOrder | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(paper_orders).where(
                        and_(
                            paper_orders.c.account_id == account_id,
                            paper_orders.c.idempotency_key == key,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        return self._to_order(row) if row else None

    def orders(self, account_id: str) -> tuple[PaperOrder, ...]:
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(paper_orders)
                    .where(paper_orders.c.account_id == account_id)
                    .order_by(paper_orders.c.created_at, paper_orders.c.order_id)
                )
                .mappings()
                .all()
            )
        return tuple(self._to_order(row) for row in rows)

    def active_locks(self, account_id: str) -> frozenset[PaperRiskLockType]:
        with self.engine.connect() as connection:
            values = (
                connection.execute(
                    select(paper_risk_locks.c.lock_type).where(
                        and_(
                            paper_risk_locks.c.account_id == account_id,
                            paper_risk_locks.c.active.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
        return frozenset(PaperRiskLockType(value) for value in values)

    def latest_risk_snapshot(self, account_id: str) -> PaperRiskSnapshot:
        """Return the latest durable view handed to the risk firewall."""

        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(paper_risk_snapshots)
                    .where(paper_risk_snapshots.c.account_id == account_id)
                    .order_by(
                        # Snapshot ids are allocated while the account row is
                        # locked.  They are the durable event order; ordering
                        # by market timestamps would allow an old event to
                        # masquerade as the current account view.
                        paper_risk_snapshots.c.snapshot_id.desc(),
                    )
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise PaperTradingError(f"missing durable risk snapshot for account: {account_id}")
        return self._to_risk_snapshot(row)

    def risk_snapshots(self, account_id: str) -> tuple[PaperRiskSnapshot, ...]:
        """Return append-only snapshots in deterministic event order."""

        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(paper_risk_snapshots)
                    .where(paper_risk_snapshots.c.account_id == account_id)
                    .order_by(paper_risk_snapshots.c.snapshot_id)
                )
                .mappings()
                .all()
            )
        return tuple(self._to_risk_snapshot(row) for row in rows)

    def audit_events(self, account_id: str) -> tuple[dict[str, Any], ...]:
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(paper_audit_events)
                    .where(paper_audit_events.c.account_id == account_id)
                    .order_by(paper_audit_events.c.timestamp, paper_audit_events.c.event_id)
                )
                .mappings()
                .all()
            )
        return tuple(dict(row) for row in rows)

    def fills(self, account_id: str) -> tuple[PaperFill, ...]:
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(paper_fills)
                    .where(paper_fills.c.account_id == account_id)
                    .order_by(paper_fills.c.timestamp, paper_fills.c.fill_id)
                )
                .mappings()
                .all()
            )
        return tuple(self._to_fill(row) for row in rows)

    def active_reservation_total_for_account(self, account_id: str) -> Decimal:
        with self.engine.connect() as connection:
            return self.active_reservation_total(connection, account_id)

    def active_reservation_intent_ids(self, account_id: str) -> frozenset[str]:
        """Return durable active intent identities for the next firewall evaluation."""

        with self.engine.connect() as connection:
            values = connection.execute(
                select(paper_reservations.c.intent_id).where(
                    and_(
                        paper_reservations.c.account_id == account_id,
                        paper_reservations.c.active.is_(True),
                    )
                )
            ).scalars()
            return frozenset(values)

    def active_reservation_order_ids(self, account_id: str) -> frozenset[str]:
        """Return durable active reservation identities for reconciliation."""

        with self.engine.connect() as connection:
            values = connection.execute(
                select(paper_reservations.c.order_id).where(
                    and_(
                        paper_reservations.c.account_id == account_id,
                        paper_reservations.c.active.is_(True),
                    )
                )
            ).scalars()
            return frozenset(values)

    def active_reservation_bindings(
        self, account_id: str
    ) -> tuple[tuple[str, str, str], ...]:
        """Return active reservation/order authorization identities."""

        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(
                        paper_reservations.c.order_id,
                        paper_reservations.c.intent_id,
                        paper_reservations.c.risk_decision_id,
                    )
                    .where(
                        and_(
                            paper_reservations.c.account_id == account_id,
                            paper_reservations.c.active.is_(True),
                        )
                    )
                    .order_by(paper_reservations.c.order_id)
                )
                .all()
            )
        return tuple(
            (str(order_id), str(intent_id), str(decision_id))
            for order_id, intent_id, decision_id in rows
        )

    # The methods below intentionally require a connection.  The engine owns
    # sequencing and invokes them in one DB transaction with an account row
    # lock; users never receive a state-mutating repository API.
    def locked_account(self, connection: Connection, account_id: str) -> PaperAccount:
        row = (
            connection.execute(
                select(paper_accounts)
                .where(paper_accounts.c.account_id == account_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise PaperTradingError(f"unknown paper account: {account_id}")
        return self._to_account(row)

    def lock_execution_authority(
        self, connection: Connection, authority: ExecutionAuthority, *, at: datetime
    ) -> None:
        """Lock and validate the lease before any operational paper mutation.

        Lock ordering for operational financial transactions is fixed as:
        ``worker_leases`` → ``paper_accounts`` → order/position/reservation
        rows.  Lease takeover uses the first row, so it either waits for the
        complete financial transaction or wins before the stale transaction
        can mutate anything.
        """

        row = (
            connection.execute(
                select(worker_leases)
                .where(worker_leases.c.workload_id == authority.lease_key)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if (
            row is None
            or row["owner_id"] != authority.owner_id
            or row["claimed_workload_id"] != authority.workload_id
            or row["fence_generation"] != authority.generation
            or row["expires_at"] is None
            or _utc(row["expires_at"]) <= at
        ):
            raise PaperTradingError("operational paper execution authority is not current")

    def locked_order_by_idempotency(
        self, connection: Connection, account_id: str, key: str
    ) -> PaperOrder | None:
        row = (
            connection.execute(
                select(paper_orders)
                .where(
                    and_(
                        paper_orders.c.account_id == account_id,
                        paper_orders.c.idempotency_key == key,
                    )
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        return self._to_order(row) if row else None

    def locked_order(self, connection: Connection, account_id: str, order_id: str) -> PaperOrder:
        row = (
            connection.execute(
                select(paper_orders)
                .where(
                    and_(
                        paper_orders.c.account_id == account_id, paper_orders.c.order_id == order_id
                    )
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise PaperTradingError(f"unknown paper order: {order_id}")
        return self._to_order(row)

    def locked_position(
        self, connection: Connection, account_id: str, symbol: str
    ) -> PaperPosition | None:
        row = (
            connection.execute(
                select(paper_positions)
                .where(
                    and_(
                        paper_positions.c.account_id == account_id,
                        paper_positions.c.symbol == symbol,
                    )
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        return self._to_position(row) if row else None

    def active_reservation_total(self, connection: Connection, account_id: str) -> Decimal:
        rows = (
            connection.execute(
                select(paper_reservations.c.reserved_risk).where(
                    and_(
                        paper_reservations.c.account_id == account_id,
                        paper_reservations.c.active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        return sum((_decimal(value) for value in rows), Decimal("0"))

    def active_reserved_cash_total(self, connection: Connection, account_id: str) -> Decimal:
        rows = (
            connection.execute(
                select(paper_reservations.c.reserved_cash).where(
                    and_(
                        paper_reservations.c.account_id == account_id,
                        paper_reservations.c.active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        return sum((_decimal(value) for value in rows), Decimal("0"))

    def locked_active_reservation(
        self, connection: Connection, order_id: str
    ) -> tuple[Decimal, Decimal]:
        row = (
            connection.execute(
                select(paper_reservations)
                .where(
                    and_(
                        paper_reservations.c.order_id == order_id,
                        paper_reservations.c.active.is_(True),
                    )
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise PaperTradingError(f"missing active reservation for open order: {order_id}")
        return _decimal(row["reserved_risk"]), _decimal(row["reserved_cash"])

    def locked_filled_totals(
        self, connection: Connection, order_id: str
    ) -> tuple[Decimal, Decimal]:
        """Return cumulative executed notional and commission for one order."""

        rows = connection.execute(
            select(paper_fills.c.quantity, paper_fills.c.price, paper_fills.c.commission)
            .where(paper_fills.c.order_id == order_id)
            .with_for_update()
        ).all()
        return (
            sum(
                (_decimal(quantity) * _decimal(price) for quantity, price, _ in rows),
                Decimal("0"),
            ),
            sum((_decimal(commission) for _, _, commission in rows), Decimal("0")),
        )

    def has_active_lock(self, connection: Connection, account_id: str) -> bool:
        return (
            connection.execute(
                select(paper_risk_locks.c.lock_id)
                .where(
                    and_(
                        paper_risk_locks.c.account_id == account_id,
                        paper_risk_locks.c.active.is_(True),
                    )
                )
                .limit(1)
            ).first()
            is not None
        )

    def ensure_instrument(self, connection: Connection, instrument: Instrument) -> None:
        existing = connection.execute(
            select(instruments.c.canonical_symbol).where(
                instruments.c.canonical_symbol == instrument.canonical_symbol
            )
        ).first()
        if existing is not None:
            return
        values = instrument.model_dump(mode="json")
        connection.execute(instruments.insert(), values)

    def transaction_positions(
        self, connection: Connection, account_id: str
    ) -> tuple[PaperPosition, ...]:
        rows = (
            connection.execute(
                select(paper_positions)
                .where(paper_positions.c.account_id == account_id)
                .order_by(paper_positions.c.symbol)
                .with_for_update()
            )
            .mappings()
            .all()
        )
        return tuple(self._to_position(row) for row in rows)

    def insert_order_and_reservation(
        self,
        connection: Connection,
        order: PaperOrder,
        *,
        reservation_id: str,
        reserved_risk: Decimal,
        reserved_cash: Decimal,
    ) -> None:
        self.ensure_instrument(connection, order.instrument)
        connection.execute(paper_orders.insert(), self._order_row(order))
        connection.execute(
            paper_reservations.insert(),
            {
                "reservation_id": reservation_id,
                "account_id": order.account_id,
                "order_id": order.order_id,
                "intent_id": order.source_intent_id,
                "risk_decision_id": order.risk_decision_id,
                "reserved_risk": reserved_risk,
                "reserved_cash": reserved_cash,
                "active": True,
                "created_at": order.created_at,
                "released_at": None,
            },
        )
        connection.execute(
            update(paper_accounts)
            .where(paper_accounts.c.account_id == order.account_id)
            .values(
                reserved_risk=paper_accounts.c.reserved_risk + reserved_risk,
                updated_at=order.created_at,
                state_revision=paper_accounts.c.state_revision + 1,
            )
        )
        self._audit(
            connection, order.account_id, "risk_decision_consumed", order.created_at, order=order
        )
        self._audit(
            connection, order.account_id, "reservation_created", order.created_at, order=order
        )
        self._audit(
            connection,
            order.account_id,
            "order_created",
            order.created_at,
            order=order,
            new_state=PaperOrderStatus.CREATED,
        )
        self._audit(
            connection,
            order.account_id,
            "order_submitted",
            order.created_at,
            order=order,
            previous_state=PaperOrderStatus.CREATED,
            new_state=PaperOrderStatus.SUBMITTED,
        )
        self._audit(
            connection,
            order.account_id,
            "order_accepted",
            order.created_at,
            order=order,
            previous_state=PaperOrderStatus.SUBMITTED,
            new_state=PaperOrderStatus.ACCEPTED,
        )
        self.record_risk_snapshot(connection, order.account_id, order.created_at)

    def locked_open_orders(
        self, connection: Connection, account_id: str, symbol: str
    ) -> tuple[PaperOrder, ...]:
        rows = (
            connection.execute(
                select(paper_orders)
                .where(
                    and_(
                        paper_orders.c.account_id == account_id,
                        paper_orders.c.symbol == symbol,
                        paper_orders.c.status.in_(
                            [
                                PaperOrderStatus.ACCEPTED.value,
                                PaperOrderStatus.PARTIALLY_FILLED.value,
                            ]
                        ),
                    )
                )
                .order_by(paper_orders.c.created_at, paper_orders.c.order_id)
                .with_for_update()
            )
            .mappings()
            .all()
        )
        return tuple(self._to_order(row) for row in rows)

    def locked_open_orders_for_account(
        self, connection: Connection, account_id: str
    ) -> tuple[PaperOrder, ...]:
        rows = (
            connection.execute(
                select(paper_orders)
                .where(
                    and_(
                        paper_orders.c.account_id == account_id,
                        paper_orders.c.status.in_(
                            [
                                PaperOrderStatus.ACCEPTED.value,
                                PaperOrderStatus.PARTIALLY_FILLED.value,
                                PaperOrderStatus.CANCEL_REQUESTED.value,
                            ]
                        ),
                    )
                )
                .order_by(paper_orders.c.created_at, paper_orders.c.order_id)
                .with_for_update()
            )
            .mappings()
            .all()
        )
        return tuple(self._to_order(row) for row in rows)

    def update_order(self, connection: Connection, order: PaperOrder, timestamp: datetime) -> None:
        current = self.locked_order(connection, order.account_id, order.order_id)
        if not current.can_transition_to(order.status):
            raise PaperTradingError(
                f"invalid durable order transition {current.status.value} → {order.status.value}"
            )
        if (
            order.idempotency_key != current.idempotency_key
            or order.account_id != current.account_id
            or order.instrument != current.instrument
            or order.side is not current.side
            or order.order_type is not current.order_type
            or order.quantity != current.quantity
            or order.limit_price != current.limit_price
            or order.stop_price != current.stop_price
            or order.time_in_force is not current.time_in_force
            or order.created_at != current.created_at
            or order.submitted_at != current.submitted_at
            or order.expires_at != current.expires_at
            or order.risk_decision_id != current.risk_decision_id
            or order.risk_policy_id != current.risk_policy_id
            or order.risk_policy_configuration_id != current.risk_policy_configuration_id
            or order.sizing_configuration_id != current.sizing_configuration_id
            or order.source_intent_id != current.source_intent_id
            or order.authorization_action is not current.authorization_action
            or order.authorization_max_new_notional != current.authorization_max_new_notional
            or order.authorization_max_reduction_notional
            != current.authorization_max_reduction_notional
            or order.authorization_costs_included is not current.authorization_costs_included
        ):
            raise PaperTradingError("order transition cannot alter immutable order authorization")
        if order.filled_quantity < current.filled_quantity:
            raise PaperTradingError("order filled quantity cannot decrease")
        if order.status in {PaperOrderStatus.PARTIALLY_FILLED, PaperOrderStatus.FILLED} and (
            order.filled_quantity <= current.filled_quantity
        ):
            raise PaperTradingError("fill transition requires newly filled quantity")
        if order.status is PaperOrderStatus.FILLED and order.filled_quantity != order.quantity:
            raise PaperTradingError("filled order must have no remaining quantity")
        connection.execute(
            update(paper_orders)
            .where(paper_orders.c.order_id == order.order_id)
            .values(
                status=order.status.value,
                filled_quantity=order.filled_quantity,
                average_fill_price=order.average_fill_price,
                rejection_reason=order.rejection_reason,
                last_market_event_at=order.last_market_event_at,
            )
        )
        self._audit(
            connection,
            order.account_id,
            "order_transition",
            timestamp,
            order=order,
            previous_state=current.status,
            new_state=order.status,
        )

    def insert_fill(self, connection: Connection, fill: PaperFill) -> None:
        self.ensure_instrument(connection, fill.instrument)
        connection.execute(
            paper_fills.insert(),
            {
                "fill_id": fill.fill_id,
                "order_id": fill.order_id,
                "account_id": fill.account_id,
                "symbol": fill.instrument.canonical_symbol,
                "instrument": fill.instrument.model_dump(mode="json"),
                "side": fill.side.value,
                "quantity": fill.quantity,
                "price": fill.price,
                "reference_price": fill.reference_price,
                "commission": fill.commission,
                "timestamp": fill.timestamp,
            },
        )
        self._audit(connection, fill.account_id, "fill_applied", fill.timestamp, fill=fill)

    def record_market_event(
        self, connection: Connection, account_id: str, order_id: str, timestamp: datetime
    ) -> None:
        connection.execute(
            update(paper_orders)
            .where(
                and_(
                    paper_orders.c.account_id == account_id,
                    paper_orders.c.order_id == order_id,
                )
            )
            .values(last_market_event_at=timestamp)
        )
        connection.execute(
            update(paper_accounts)
            .where(paper_accounts.c.account_id == account_id)
            .values(updated_at=timestamp, state_revision=paper_accounts.c.state_revision + 1)
        )

    def upsert_position(self, connection: Connection, position: PaperPosition) -> None:
        row = {
            "account_id": position.account_id,
            "symbol": position.instrument.canonical_symbol,
            "instrument": position.instrument.model_dump(mode="json"),
            "quantity": position.quantity,
            "average_entry_price": position.average_entry_price,
            "market_price": position.market_price,
            "realized_pnl": position.realized_pnl,
            "unrealized_pnl": position.unrealized_pnl,
            "fees": position.fees,
            "updated_at": position.updated_at,
        }
        existing = connection.execute(
            select(paper_positions.c.symbol).where(
                and_(
                    paper_positions.c.account_id == position.account_id,
                    paper_positions.c.symbol == position.instrument.canonical_symbol,
                )
            )
        ).first()
        if existing is None:
            self.ensure_instrument(connection, position.instrument)
            connection.execute(paper_positions.insert(), row)
        else:
            connection.execute(
                update(paper_positions)
                .where(
                    and_(
                        paper_positions.c.account_id == position.account_id,
                        paper_positions.c.symbol == position.instrument.canonical_symbol,
                    )
                )
                .values(**row)
            )

    def update_account_projection(
        self,
        connection: Connection,
        account: PaperAccount,
        *,
        cash: Decimal,
        realized_pnl: Decimal,
        fees: Decimal,
        high_water_mark: Decimal,
        risk_day: date,
        risk_day_starting_equity: Decimal,
        timestamp: datetime,
    ) -> None:
        connection.execute(
            update(paper_accounts)
            .where(paper_accounts.c.account_id == account.account_id)
            .values(
                cash=cash,
                realized_pnl=realized_pnl,
                fees=fees,
                high_water_mark=high_water_mark,
                risk_day=risk_day.isoformat(),
                risk_day_starting_equity=risk_day_starting_equity,
                updated_at=timestamp,
                state_revision=paper_accounts.c.state_revision + 1,
            )
        )

    def release_reservation(
        self, connection: Connection, order: PaperOrder, timestamp: datetime
    ) -> None:
        row = (
            connection.execute(
                select(paper_reservations)
                .where(
                    and_(
                        paper_reservations.c.order_id == order.order_id,
                        paper_reservations.c.active.is_(True),
                    )
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return
        reserved_risk = _decimal(row["reserved_risk"])
        connection.execute(
            update(paper_reservations)
            .where(paper_reservations.c.reservation_id == row["reservation_id"])
            .values(active=False, released_at=timestamp)
        )
        connection.execute(
            update(paper_accounts)
            .where(paper_accounts.c.account_id == order.account_id)
            .values(
                reserved_risk=paper_accounts.c.reserved_risk - reserved_risk,
                updated_at=timestamp,
                state_revision=paper_accounts.c.state_revision + 1,
            )
        )
        self._audit(connection, order.account_id, "reservation_released", timestamp, order=order)

    def activate_lock(
        self,
        connection: Connection,
        account_id: str,
        lock_type: PaperRiskLockType,
        reason: str,
        timestamp: datetime,
    ) -> None:
        existing = connection.execute(
            select(paper_risk_locks.c.lock_id).where(
                and_(
                    paper_risk_locks.c.account_id == account_id,
                    paper_risk_locks.c.lock_type == lock_type.value,
                    paper_risk_locks.c.active.is_(True),
                )
            )
        ).first()
        if existing is None:
            lock_id = f"{account_id}:{lock_type.value}"
            connection.execute(
                paper_risk_locks.insert(),
                {
                    "lock_id": lock_id,
                    "account_id": account_id,
                    "lock_type": lock_type.value,
                    "reason": reason,
                    "active": True,
                    "created_at": timestamp,
                    "cleared_at": None,
                },
            )
            connection.execute(
                update(paper_accounts)
                .where(paper_accounts.c.account_id == account_id)
                .values(
                    updated_at=timestamp,
                    state_revision=paper_accounts.c.state_revision + 1,
                )
            )
            self._audit(connection, account_id, "risk_lock_activated", timestamp, reason=reason)

    def record_risk_snapshot(
        self, connection: Connection, account_id: str, timestamp: datetime
    ) -> PaperRiskSnapshot:
        """Persist a complete risk view inside the caller's account transaction.

        Account, position, order, reservation, and lock records remain the
        authoritative state.  This append-only projection makes the Phase 7
        hand-off explicit and exposes the age of marks used in equity.
        """

        account = self.locked_account(connection, account_id)
        positions = self.transaction_positions(connection, account_id)
        lock_values = connection.execute(
            select(paper_risk_locks.c.lock_type).where(
                and_(
                    paper_risk_locks.c.account_id == account_id,
                    paper_risk_locks.c.active.is_(True),
                )
            )
        ).scalars()
        locks = tuple(
            sorted((PaperRiskLockType(value) for value in lock_values), key=lambda item: item.value)
        )
        gross = sum((abs(item.quantity * item.market_price) for item in positions), Decimal("0"))
        net = sum((item.quantity * item.market_price for item in positions), Decimal("0"))
        long = sum(
            (item.quantity * item.market_price for item in positions if item.quantity > 0),
            Decimal("0"),
        )
        short = sum(
            (-item.quantity * item.market_price for item in positions if item.quantity < 0),
            Decimal("0"),
        )
        unrealized = sum((item.unrealized_pnl for item in positions), Decimal("0"))
        equity = account.cash + net
        pending_order_count = len(
            connection.execute(
                select(paper_orders.c.order_id).where(
                    and_(
                        paper_orders.c.account_id == account_id,
                        paper_orders.c.status.in_(
                            [
                                PaperOrderStatus.ACCEPTED.value,
                                PaperOrderStatus.PARTIALLY_FILLED.value,
                                PaperOrderStatus.CANCEL_REQUESTED.value,
                            ]
                        ),
                    )
                )
            ).all()
        )
        marked = [item.updated_at for item in positions if item.quantity != 0]
        existing = connection.execute(
            select(paper_risk_snapshots.c.snapshot_id).where(
                paper_risk_snapshots.c.account_id == account_id
            )
        ).all()
        snapshot = PaperRiskSnapshot(
            snapshot_id=f"{account_id}:{len(existing) + 1:012d}",
            account_id=account_id,
            timestamp=timestamp,
            cash=account.cash,
            equity=equity,
            used_margin=Decimal("0"),
            available_margin=max(equity, Decimal("0")),
            gross_exposure=gross,
            net_exposure=net,
            long_exposure=long,
            short_exposure=short,
            realized_pnl=account.realized_pnl,
            unrealized_pnl=unrealized,
            fees=account.fees,
            daily_pnl=equity - account.risk_day_starting_equity,
            high_water_mark=account.high_water_mark,
            drawdown=max(account.high_water_mark - equity, Decimal("0")),
            reserved_risk=account.reserved_risk,
            pending_order_count=pending_order_count,
            active_locks=locks,
            mark_timestamp=min(marked) if marked else None,
            positions=positions,
        )
        connection.execute(
            paper_risk_snapshots.insert(),
            {
                "snapshot_id": snapshot.snapshot_id,
                "account_id": snapshot.account_id,
                "timestamp": snapshot.timestamp,
                "cash": snapshot.cash,
                "equity": snapshot.equity,
                "used_margin": snapshot.used_margin,
                "available_margin": snapshot.available_margin,
                "gross_exposure": snapshot.gross_exposure,
                "net_exposure": snapshot.net_exposure,
                "long_exposure": snapshot.long_exposure,
                "short_exposure": snapshot.short_exposure,
                "realized_pnl": snapshot.realized_pnl,
                "unrealized_pnl": snapshot.unrealized_pnl,
                "fees": snapshot.fees,
                "daily_pnl": snapshot.daily_pnl,
                "high_water_mark": snapshot.high_water_mark,
                "drawdown": snapshot.drawdown,
                "reserved_risk": snapshot.reserved_risk,
                "pending_order_count": snapshot.pending_order_count,
                "active_locks": [item.value for item in snapshot.active_locks],
                "mark_timestamp": snapshot.mark_timestamp,
                "positions": [self._risk_position_row(item) for item in snapshot.positions],
            },
        )
        self._audit(connection, account_id, "risk_snapshot_created", timestamp)
        return snapshot

    def _audit(
        self,
        connection: Connection,
        account_id: str,
        event_type: str,
        timestamp: datetime,
        *,
        order: PaperOrder | None = None,
        fill: PaperFill | None = None,
        previous_state: PaperOrderStatus | None = None,
        new_state: PaperOrderStatus | None = None,
        reason: str | None = None,
    ) -> None:
        count = connection.execute(
            select(paper_audit_events.c.event_id).where(
                paper_audit_events.c.account_id == account_id
            )
        ).all()
        event_id = f"{account_id}:{len(count) + 1:012d}"
        connection.execute(
            paper_audit_events.insert(),
            {
                "event_id": event_id,
                "account_id": account_id,
                "order_id": order.order_id if order else (fill.order_id if fill else None),
                "fill_id": fill.fill_id if fill else None,
                "intent_id": order.source_intent_id if order else None,
                "risk_decision_id": order.risk_decision_id if order else None,
                "event_type": event_type,
                "previous_state": previous_state.value if previous_state else None,
                "new_state": new_state.value if new_state else None,
                "reason": reason,
                "timestamp": timestamp,
                "metadata": {},
            },
        )

    @staticmethod
    def _account_row(value: PaperAccount) -> dict[str, Any]:
        return {
            "account_id": value.account_id,
            "account_currency": value.account_currency,
            "starting_cash": value.starting_cash,
            "cash": value.cash,
            "reserved_risk": value.reserved_risk,
            "risk_capacity": value.risk_capacity,
            "daily_loss_limit": value.daily_loss_limit,
            "max_drawdown": value.max_drawdown,
            "risk_policy_id": value.risk_policy_id,
            "risk_policy_configuration_id": value.risk_policy_configuration_id,
            "risk_day": value.risk_day.isoformat(),
            "risk_day_starting_equity": value.risk_day_starting_equity,
            "high_water_mark": value.high_water_mark,
            "realized_pnl": value.realized_pnl,
            "fees": value.fees,
            "created_at": value.created_at,
            "updated_at": value.updated_at,
            "state_revision": value.state_revision,
        }

    @staticmethod
    def _order_row(value: PaperOrder) -> dict[str, Any]:
        return {
            "order_id": value.order_id,
            "idempotency_key": value.idempotency_key,
            "account_id": value.account_id,
            "symbol": value.instrument.canonical_symbol,
            "instrument": value.instrument.model_dump(mode="json"),
            "side": value.side.value,
            "order_type": value.order_type.value,
            "quantity": value.quantity,
            "filled_quantity": value.filled_quantity,
            "average_fill_price": value.average_fill_price,
            "limit_price": value.limit_price,
            "stop_price": value.stop_price,
            "time_in_force": value.time_in_force.value,
            "created_at": value.created_at,
            "submitted_at": value.submitted_at,
            "expires_at": value.expires_at,
            "last_market_event_at": value.last_market_event_at,
            "risk_decision_id": value.risk_decision_id,
            "risk_policy_id": value.risk_policy_id,
            "risk_policy_configuration_id": value.risk_policy_configuration_id,
            "sizing_configuration_id": value.sizing_configuration_id,
            "source_intent_id": value.source_intent_id,
            "authorization_action": value.authorization_action.value,
            "authorization_max_new_notional": value.authorization_max_new_notional,
            "authorization_max_reduction_notional": value.authorization_max_reduction_notional,
            "authorization_costs_included": value.authorization_costs_included,
            "status": value.status.value,
            "rejection_reason": value.rejection_reason,
        }

    @staticmethod
    def _to_account(row: RowMapping) -> PaperAccount:
        return PaperAccount(
            account_id=row["account_id"],
            account_currency=row["account_currency"],
            starting_cash=_decimal(row["starting_cash"]),
            cash=_decimal(row["cash"]),
            reserved_risk=_decimal(row["reserved_risk"]),
            risk_capacity=_decimal(row["risk_capacity"]),
            daily_loss_limit=_decimal(row["daily_loss_limit"]),
            max_drawdown=_decimal(row["max_drawdown"]),
            risk_policy_id=row["risk_policy_id"],
            risk_policy_configuration_id=row["risk_policy_configuration_id"],
            risk_day=date.fromisoformat(row["risk_day"]),
            risk_day_starting_equity=_decimal(row["risk_day_starting_equity"]),
            high_water_mark=_decimal(row["high_water_mark"]),
            realized_pnl=_decimal(row["realized_pnl"]),
            fees=_decimal(row["fees"]),
            created_at=_utc(row["created_at"]),
            updated_at=_utc(row["updated_at"]),
            state_revision=row["state_revision"] if row["state_revision"] is not None else 0,
        )

    @staticmethod
    def _to_position(row: RowMapping) -> PaperPosition:
        return PaperPosition(
            account_id=row["account_id"],
            instrument=Instrument(**row["instrument"]),
            quantity=_decimal(row["quantity"]),
            average_entry_price=_decimal(row["average_entry_price"]),
            market_price=_decimal(row["market_price"]),
            realized_pnl=_decimal(row["realized_pnl"]),
            unrealized_pnl=_decimal(row["unrealized_pnl"]),
            fees=_decimal(row["fees"]),
            updated_at=_utc(row["updated_at"]),
        )

    @staticmethod
    def _to_risk_snapshot(row: RowMapping) -> PaperRiskSnapshot:
        return PaperRiskSnapshot(
            snapshot_id=row["snapshot_id"],
            account_id=row["account_id"],
            timestamp=_utc(row["timestamp"]),
            cash=_decimal(row["cash"]),
            equity=_decimal(row["equity"]),
            used_margin=_decimal(row["used_margin"]),
            available_margin=_decimal(row["available_margin"]),
            gross_exposure=_decimal(row["gross_exposure"]),
            net_exposure=_decimal(row["net_exposure"]),
            long_exposure=_decimal(row["long_exposure"]),
            short_exposure=_decimal(row["short_exposure"]),
            realized_pnl=_decimal(row["realized_pnl"]),
            unrealized_pnl=_decimal(row["unrealized_pnl"]),
            fees=_decimal(row["fees"]),
            daily_pnl=_decimal(row["daily_pnl"]),
            high_water_mark=_decimal(row["high_water_mark"]),
            drawdown=_decimal(row["drawdown"]),
            reserved_risk=_decimal(row["reserved_risk"]),
            pending_order_count=row["pending_order_count"],
            active_locks=tuple(PaperRiskLockType(value) for value in row["active_locks"]),
            mark_timestamp=_utc(row["mark_timestamp"]) if row["mark_timestamp"] else None,
            positions=tuple(
                SqlAlchemyPaperStore._risk_position_from_row(item) for item in row["positions"]
            ),
        )

    @staticmethod
    def _risk_position_row(position: PaperPosition) -> dict[str, Any]:
        return {
            "account_id": position.account_id,
            "instrument": position.instrument.model_dump(mode="json"),
            "quantity": str(position.quantity),
            "average_entry_price": str(position.average_entry_price),
            "market_price": str(position.market_price),
            "realized_pnl": str(position.realized_pnl),
            "unrealized_pnl": str(position.unrealized_pnl),
            "fees": str(position.fees),
            "updated_at": position.updated_at.isoformat(),
        }

    @staticmethod
    def _risk_position_from_row(row: dict[str, Any]) -> PaperPosition:
        return PaperPosition(
            account_id=row["account_id"],
            instrument=Instrument(**row["instrument"]),
            quantity=_decimal(row["quantity"]),
            average_entry_price=_decimal(row["average_entry_price"]),
            market_price=_decimal(row["market_price"]),
            realized_pnl=_decimal(row["realized_pnl"]),
            unrealized_pnl=_decimal(row["unrealized_pnl"]),
            fees=_decimal(row["fees"]),
            updated_at=_utc(datetime.fromisoformat(row["updated_at"])),
        )

    @staticmethod
    def _to_order(row: RowMapping) -> PaperOrder:
        return PaperOrder(
            order_id=row["order_id"],
            idempotency_key=row["idempotency_key"],
            account_id=row["account_id"],
            instrument=Instrument(**row["instrument"]),
            side=OrderSide(row["side"]),
            order_type=OrderType(row["order_type"]),
            quantity=_decimal(row["quantity"]),
            filled_quantity=_decimal(row["filled_quantity"]),
            average_fill_price=_decimal(row["average_fill_price"])
            if row["average_fill_price"] is not None
            else None,
            limit_price=_decimal(row["limit_price"]) if row["limit_price"] is not None else None,
            stop_price=_decimal(row["stop_price"]) if row["stop_price"] is not None else None,
            time_in_force=TimeInForce(row["time_in_force"]),
            created_at=_utc(row["created_at"]),
            submitted_at=_utc(row["submitted_at"]),
            expires_at=_utc(row["expires_at"]) if row["expires_at"] else None,
            last_market_event_at=(
                _utc(row["last_market_event_at"]) if row["last_market_event_at"] else None
            ),
            risk_decision_id=row["risk_decision_id"],
            risk_policy_id=row["risk_policy_id"],
            risk_policy_configuration_id=row["risk_policy_configuration_id"],
            sizing_configuration_id=row["sizing_configuration_id"],
            source_intent_id=row["source_intent_id"],
            authorization_action=RiskAction(row["authorization_action"]),
            status=PaperOrderStatus(row["status"]),
            rejection_reason=row["rejection_reason"],
            authorization_max_new_notional=_decimal(row["authorization_max_new_notional"]),
            authorization_max_reduction_notional=_decimal(
                row["authorization_max_reduction_notional"]
            ),
            authorization_costs_included=bool(row["authorization_costs_included"]),
        )

    @staticmethod
    def _to_fill(row: RowMapping) -> PaperFill:
        return PaperFill(
            fill_id=row["fill_id"],
            order_id=row["order_id"],
            account_id=row["account_id"],
            instrument=Instrument(**row["instrument"]),
            side=OrderSide(row["side"]),
            quantity=_decimal(row["quantity"]),
            price=_decimal(row["price"]),
            reference_price=_decimal(row["reference_price"]),
            commission=_decimal(row["commission"]),
            timestamp=_utc(row["timestamp"]),
        )


__all__ = ["SqlAlchemyPaperStore"]
