"""Stateful, deterministic, offline paper execution orchestration."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from traderos.backtesting.models import OrderSide, OrderType, TimeInForce
from traderos.data.time import require_utc
from traderos.paper.models import (
    PaperAccount,
    PaperExecutionConfig,
    PaperFill,
    PaperOrder,
    PaperOrderStatus,
    PaperPosition,
    PaperQuote,
    PaperRiskLockType,
    PaperTradingError,
    quantity_for_authorization,
)
from traderos.paper.store import SqlAlchemyPaperStore
from traderos.risk.models import (
    RiskAction,
    RiskDecision,
    RiskDecisionStatus,
    risk_decision_integrity_id,
)


class PaperTradingEngine:
    """The sole supported ``RiskDecision → PaperOrder`` path.

    It has no public method for direct order creation, direct fill injection, or
    direct account/position mutation.  Market data is supplied by callers as a
    validated explicit quote; this engine never fetches it.
    """

    def __init__(
        self, store: SqlAlchemyPaperStore, config: PaperExecutionConfig | None = None
    ) -> None:
        self.store = store
        self.config = config or PaperExecutionConfig()

    def create_account(
        self,
        *,
        account_id: str,
        account_currency: str,
        starting_cash: Decimal,
        risk_capacity: Decimal,
        daily_loss_limit: Decimal,
        max_drawdown: Decimal,
        risk_policy_id: str,
        risk_policy_configuration_id: str,
        timestamp: datetime,
    ) -> PaperAccount:
        require_utc(timestamp)
        return self.store.create_account(
            PaperAccount(
                account_id=account_id,
                account_currency=account_currency,
                starting_cash=starting_cash,
                cash=starting_cash,
                reserved_risk=Decimal("0"),
                risk_capacity=risk_capacity,
                daily_loss_limit=daily_loss_limit,
                max_drawdown=max_drawdown,
                risk_policy_id=risk_policy_id,
                risk_policy_configuration_id=risk_policy_configuration_id,
                risk_day=timestamp.date(),
                risk_day_starting_equity=starting_cash,
                high_water_mark=starting_cash,
                realized_pnl=Decimal("0"),
                fees=Decimal("0"),
                created_at=timestamp,
                updated_at=timestamp,
            )
        )

    def submit(
        self,
        *,
        account_id: str,
        idempotency_key: str,
        risk_decision: RiskDecision,
        quote: PaperQuote,
        timestamp: datetime,
        order_type: OrderType = OrderType.MARKET,
        time_in_force: TimeInForce = TimeInForce.GTC,
        limit_price: Decimal | None = None,
        stop_price: Decimal | None = None,
        expires_at: datetime | None = None,
    ) -> PaperOrder:
        """Atomically consume an approved immutable decision and reserve capacity."""

        require_utc(timestamp)
        if not idempotency_key.strip():
            raise PaperTradingError("idempotency key must not be blank")
        self._validate_decision(risk_decision, quote, timestamp, account_id)
        if expires_at is not None:
            require_utc(expires_at)
            if expires_at <= timestamp:
                raise PaperTradingError("expiration must follow submission")
        try:
            with self.store.engine.begin() as connection:
                account = self.store.locked_account(connection, account_id)
                existing = self.store.locked_order_by_idempotency(
                    connection, account_id, idempotency_key
                )
                if existing is not None:
                    if existing.risk_decision_id != risk_decision.decision_id:
                        raise PaperTradingError(
                            "idempotency key was already used for another decision"
                        )
                    return existing
                self._validate_account_authorization(account, risk_decision)
                position = self.store.locked_position(
                    connection, account_id, risk_decision.instrument.canonical_symbol
                )
                if self.store.has_active_lock(connection, account_id) and (
                    risk_decision.authorization is None
                    or risk_decision.authorization.action is not RiskAction.REDUCTION_ONLY
                ):
                    raise PaperTradingError(
                        "active durable risk lock blocks risk-increasing paper order"
                    )
                sized = quantity_for_authorization(
                    decision=risk_decision,
                    quote=quote,
                    account=account,
                    position=position,
                    reserved_risk=self.store.active_reservation_total(connection, account_id),
                    reserved_cash=self.store.active_reserved_cash_total(connection, account_id),
                    config=self.config,
                    current_gross_exposure=self._gross_exposure(connection, account_id),
                )
                side = OrderSide.BUY if risk_decision.direction.value == "long" else OrderSide.SELL
                if risk_decision.authorization is None:
                    raise PaperTradingError("missing authorization")
                order_id = self._order_id(account_id, idempotency_key, risk_decision.decision_id)
                order = PaperOrder(
                    order_id=order_id,
                    idempotency_key=idempotency_key,
                    account_id=account_id,
                    instrument=risk_decision.instrument,
                    side=side,
                    order_type=order_type,
                    quantity=sized.quantity,
                    filled_quantity=Decimal("0"),
                    average_fill_price=None,
                    limit_price=limit_price,
                    stop_price=stop_price,
                    time_in_force=time_in_force,
                    created_at=timestamp,
                    submitted_at=timestamp,
                    expires_at=expires_at,
                    last_market_event_at=None,
                    risk_decision_id=risk_decision.decision_id,
                    risk_policy_id=risk_decision.policy_id,
                    risk_policy_configuration_id=risk_decision.configuration_id,
                    sizing_configuration_id=sized.configuration_id,
                    source_intent_id=risk_decision.intent_id,
                    authorization_action=risk_decision.authorization.action,
                    status=PaperOrderStatus.ACCEPTED,
                )
                self.store.insert_order_and_reservation(
                    connection,
                    order,
                    reservation_id=f"reservation:{order_id}",
                    reserved_risk=sized.reserved_risk,
                    reserved_cash=sized.reserved_cash,
                )
                return order
        except IntegrityError as exc:
            # A unique constraint is a second line of defence against retries
            # and cross-process races.  Return the established order when the
            # caller retried exactly the same logical request.
            existing = self.store.order_by_idempotency(account_id, idempotency_key)
            if existing is not None and existing.risk_decision_id == risk_decision.decision_id:
                return existing
            raise PaperTradingError("durable order/reservation uniqueness conflict") from exc
        except SQLAlchemyError as exc:
            raise PaperTradingError("paper submission transaction failed") from exc

    def process_quote(
        self, *, account_id: str, quote: PaperQuote, timestamp: datetime
    ) -> tuple[PaperFill, ...]:
        """Apply fills only from a later supplied quote; no same-event fill exists."""

        require_utc(timestamp)
        if quote.timestamp > timestamp or timestamp - quote.timestamp > self.config.quote_max_age:
            raise PaperTradingError("paper quote is future-dated or stale")
        fills: list[PaperFill] = []
        try:
            with self.store.engine.begin() as connection:
                account = self.store.locked_account(connection, account_id)
                account = self._roll_risk_day_if_needed(connection, account, quote.timestamp)
                for order in self.store.locked_open_orders(
                    connection, account_id, quote.instrument.canonical_symbol
                ):
                    if quote.timestamp <= order.submitted_at or (
                        order.last_market_event_at is not None
                        and quote.timestamp <= order.last_market_event_at
                    ):
                        continue
                    if order.expires_at is not None and quote.timestamp >= order.expires_at:
                        expired = replace(order, status=PaperOrderStatus.EXPIRED)
                        self.store.update_order(connection, expired, quote.timestamp)
                        self.store.release_reservation(connection, expired, quote.timestamp)
                        continue
                    candidate = self._execution_price(order, quote)
                    if candidate is None:
                        self.store.record_market_event(connection, order.order_id, quote.timestamp)
                        if order.time_in_force is TimeInForce.IOC:
                            expired = replace(order, status=PaperOrderStatus.EXPIRED)
                            self.store.update_order(connection, expired, quote.timestamp)
                            self.store.release_reservation(connection, expired, quote.timestamp)
                        continue
                    reference, price = candidate
                    quantity = order.remaining_quantity
                    if self.config.max_fill_quantity is not None:
                        quantity = min(quantity, self.config.max_fill_quantity)
                    if quantity <= 0:
                        continue
                    commission = max(
                        self.config.minimum_commission,
                        self.config.commission_per_unit * quantity
                        + self.config.commission_rate * quantity * price,
                    )
                    fill = PaperFill(
                        fill_id=f"{order.order_id}:{order.filled_quantity + quantity}",
                        order_id=order.order_id,
                        account_id=account_id,
                        instrument=order.instrument,
                        side=order.side,
                        quantity=quantity,
                        price=price,
                        reference_price=reference,
                        commission=commission,
                        timestamp=quote.timestamp,
                    )
                    position = self.store.locked_position(
                        connection, account_id, order.instrument.canonical_symbol
                    )
                    next_position, cash_delta, realized = self._apply_fill(position, fill)
                    if account.cash + cash_delta < 0:
                        rejected = replace(
                            order,
                            status=PaperOrderStatus.REJECTED,
                            rejection_reason="insufficient_cash_at_fill",
                        )
                        self.store.update_order(connection, rejected, quote.timestamp)
                        self.store.release_reservation(connection, rejected, quote.timestamp)
                        continue
                    if order.filled_quantity == 0:
                        weighted_price = price
                    else:
                        if order.average_fill_price is None:
                            raise PaperTradingError(
                                "partially filled order lacks average fill price"
                            )
                        weighted_price = (
                            order.average_fill_price * order.filled_quantity + price * quantity
                        ) / (order.filled_quantity + quantity)
                    filled = order.filled_quantity + quantity
                    new_status = (
                        PaperOrderStatus.FILLED
                        if filled == order.quantity
                        else PaperOrderStatus.PARTIALLY_FILLED
                    )
                    updated_order = replace(
                        order,
                        filled_quantity=filled,
                        average_fill_price=weighted_price,
                        status=new_status,
                        last_market_event_at=quote.timestamp,
                    )
                    self.store.insert_fill(connection, fill)
                    self.store.upsert_position(connection, next_position)
                    cash = account.cash + cash_delta
                    equity = self._equity(connection, account_id, cash, override=next_position)
                    high_water_mark = max(account.high_water_mark, equity)
                    self.store.update_account_projection(
                        connection,
                        account,
                        cash=cash,
                        realized_pnl=account.realized_pnl + realized,
                        fees=account.fees + commission,
                        high_water_mark=high_water_mark,
                        risk_day=quote.timestamp.date(),
                        risk_day_starting_equity=(
                            account.risk_day_starting_equity
                            if account.risk_day == quote.timestamp.date()
                            else equity
                        ),
                        timestamp=quote.timestamp,
                    )
                    self.store.update_order(connection, updated_order, quote.timestamp)
                    if new_status is PaperOrderStatus.FILLED:
                        self.store.release_reservation(connection, updated_order, quote.timestamp)
                    account = replace(
                        account,
                        cash=cash,
                        realized_pnl=account.realized_pnl + realized,
                        fees=account.fees + commission,
                        high_water_mark=high_water_mark,
                        updated_at=quote.timestamp,
                    )
                    self._apply_loss_locks(connection, account, equity, quote.timestamp)
                    fills.append(fill)
        except IntegrityError as exc:
            raise PaperTradingError("duplicate or invalid paper fill") from exc
        except SQLAlchemyError as exc:
            raise PaperTradingError("paper fill transaction failed") from exc
        return tuple(fills)

    def cancel(
        self, *, account_id: str, order_id: str, timestamp: datetime, reason: str
    ) -> PaperOrder:
        require_utc(timestamp)
        if not reason.strip():
            raise PaperTradingError("cancellation reason must not be blank")
        with self.store.engine.begin() as connection:
            self.store.locked_account(connection, account_id)
            order = self.store.locked_order(connection, account_id, order_id)
            if order.status not in {PaperOrderStatus.ACCEPTED, PaperOrderStatus.PARTIALLY_FILLED}:
                raise PaperTradingError("only open orders can be cancelled")
            requested = replace(order, status=PaperOrderStatus.CANCEL_REQUESTED)
            self.store.update_order(connection, requested, timestamp)
            cancelled = replace(
                requested, status=PaperOrderStatus.CANCELLED, rejection_reason=reason
            )
            self.store.update_order(connection, cancelled, timestamp)
            self.store.release_reservation(connection, cancelled, timestamp)
            return cancelled

    def activate_risk_lock(
        self, *, account_id: str, lock_type: PaperRiskLockType, reason: str, timestamp: datetime
    ) -> None:
        require_utc(timestamp)
        if not reason.strip():
            raise PaperTradingError("risk lock reason must not be blank")
        with self.store.engine.begin() as connection:
            self.store.locked_account(connection, account_id)
            self.store.activate_lock(connection, account_id, lock_type, reason, timestamp)

    def recover(
        self, account_id: str
    ) -> tuple[PaperAccount, tuple[PaperPosition, ...], tuple[PaperOrder, ...]]:
        """Read durable authoritative state after a process restart.

        Fill rows are append-only and projections were committed in the same
        transaction.  Reconciliation is deliberately separate and fail-loud.
        """

        return (
            self.store.account(account_id),
            self.store.positions(account_id),
            self.store.orders(account_id),
        )

    def reconcile(self, account_id: str) -> None:
        """Detect projection/ledger inconsistencies without auto-repairing them."""

        account, positions, orders = self.recover(account_id)
        fills = self.store.fills(account_id)
        if any(fill.quantity <= 0 for fill in fills):
            raise PaperTradingError("non-positive persisted fill")
        for order in orders:
            if order.filled_quantity < 0 or order.filled_quantity > order.quantity:
                raise PaperTradingError("persisted order quantity conservation failed")
            filled = sum(
                (fill.quantity for fill in fills if fill.order_id == order.order_id), Decimal("0")
            )
            if filled != order.filled_quantity:
                raise PaperTradingError("order filled quantity differs from fill ledger")
            if order.status is PaperOrderStatus.FILLED and order.remaining_quantity != 0:
                raise PaperTradingError("filled order retains quantity")
        reservations = self.store.active_reservation_total_for_account(account_id)
        if account.reserved_risk != reservations:
            raise PaperTradingError("account reserved risk differs from reservation ledger")
        if any(not position.quantity.is_finite() for position in positions):
            raise PaperTradingError("invalid position projection")

    def _validate_decision(
        self, decision: RiskDecision, quote: PaperQuote, timestamp: datetime, account_id: str
    ) -> None:
        if decision.decision_id != risk_decision_integrity_id(decision):
            raise PaperTradingError("risk decision authorization integrity check failed")
        if decision.status is not RiskDecisionStatus.APPROVE or decision.authorization is None:
            raise PaperTradingError("paper orders require an approved risk decision")
        require_utc(decision.decision_timestamp)
        if decision.decision_timestamp > timestamp or quote.timestamp > timestamp:
            raise PaperTradingError("future-dated risk decision or quote")
        if timestamp - decision.decision_timestamp > self.config.decision_max_age:
            raise PaperTradingError("risk decision is stale")
        if timestamp - quote.timestamp > self.config.quote_max_age:
            raise PaperTradingError("quote is stale")
        if decision.instrument != quote.instrument:
            raise PaperTradingError("risk decision instrument does not match quote")
        if decision.account_id != account_id:
            raise PaperTradingError("risk decision account does not match paper account")
        if decision.direction.value not in {"long", "short"}:
            raise PaperTradingError("risk decision direction is not executable")

    @staticmethod
    def _validate_account_authorization(account: PaperAccount, decision: RiskDecision) -> None:
        if (
            decision.policy_id != account.risk_policy_id
            or decision.configuration_id != account.risk_policy_configuration_id
        ):
            raise PaperTradingError("risk decision policy identity does not match account")

    @staticmethod
    def _order_id(account_id: str, key: str, decision_id: str) -> str:
        digest = hashlib.sha256(f"{account_id}|{key}|{decision_id}".encode()).hexdigest()[:24]
        return f"paper-{digest}"

    def _execution_price(
        self, order: PaperOrder, quote: PaperQuote
    ) -> tuple[Decimal, Decimal] | None:
        executable = quote.ask if order.side is OrderSide.BUY else quote.bid
        if order.order_type is OrderType.LIMIT:
            assert order.limit_price is not None
            if (order.side is OrderSide.BUY and executable > order.limit_price) or (
                order.side is OrderSide.SELL and executable < order.limit_price
            ):
                return None
            return executable, executable
        if order.order_type is OrderType.STOP:
            assert order.stop_price is not None
            if (order.side is OrderSide.BUY and executable < order.stop_price) or (
                order.side is OrderSide.SELL and executable > order.stop_price
            ):
                return None
        price = (
            executable + self.config.slippage_absolute
            if order.side is OrderSide.BUY
            else executable - self.config.slippage_absolute
        )
        if not price.is_finite() or price <= 0:
            raise PaperTradingError("slippage produced invalid execution price")
        return executable, price

    @staticmethod
    def _apply_fill(
        position: PaperPosition | None, fill: PaperFill
    ) -> tuple[PaperPosition, Decimal, Decimal]:
        previous = position or PaperPosition(
            account_id=fill.account_id,
            instrument=fill.instrument,
            quantity=Decimal("0"),
            average_entry_price=Decimal("0"),
            market_price=Decimal("0"),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            fees=Decimal("0"),
            updated_at=fill.timestamp,
        )
        signed = fill.quantity if fill.side is OrderSide.BUY else -fill.quantity
        old = previous.quantity
        average = previous.average_entry_price
        realized = Decimal("0")
        if old == 0 or old * signed > 0:
            quantity = old + signed
            average = (
                fill.price
                if old == 0
                else (average * abs(old) + fill.price * abs(signed)) / abs(quantity)
            )
        else:
            closed = min(abs(old), abs(signed))
            realized = (
                (fill.price - average) * closed * (Decimal("1") if old > 0 else Decimal("-1"))
            )
            quantity = old + signed
            if quantity == 0:
                average = Decimal("0")
            elif old * quantity < 0:
                average = fill.price
        unrealized = (fill.price - average) * quantity if quantity else Decimal("0")
        updated = PaperPosition(
            account_id=fill.account_id,
            instrument=fill.instrument,
            quantity=quantity,
            average_entry_price=average,
            market_price=fill.price,
            realized_pnl=previous.realized_pnl + realized,
            unrealized_pnl=unrealized,
            fees=previous.fees + fill.commission,
            updated_at=fill.timestamp,
        )
        cash_delta = -signed * fill.price - fill.commission
        return updated, cash_delta, realized

    def _equity(
        self, connection: Connection, account_id: str, cash: Decimal, *, override: PaperPosition
    ) -> Decimal:
        positions = self.store.transaction_positions(connection, account_id)
        projected = {item.instrument.canonical_symbol: item for item in positions}
        projected[override.instrument.canonical_symbol] = override
        return cash + sum(
            (item.quantity * item.market_price for item in projected.values()), Decimal("0")
        )

    def _gross_exposure(self, connection: Connection, account_id: str) -> Decimal:
        """Return durable marked gross notional used by the reservation boundary."""

        return sum(
            (
                abs(position.quantity * position.market_price)
                for position in self.store.transaction_positions(connection, account_id)
            ),
            Decimal("0"),
        )

    def _roll_risk_day_if_needed(
        self, connection: Connection, account: PaperAccount, timestamp: datetime
    ) -> PaperAccount:
        if account.risk_day == timestamp.date():
            return account
        positions = self.store.transaction_positions(connection, account.account_id)
        equity = account.cash + sum(
            (position.quantity * position.market_price for position in positions), Decimal("0")
        )
        self.store.update_account_projection(
            connection,
            account,
            cash=account.cash,
            realized_pnl=account.realized_pnl,
            fees=account.fees,
            high_water_mark=account.high_water_mark,
            risk_day=timestamp.date(),
            risk_day_starting_equity=equity,
            timestamp=timestamp,
        )
        return replace(
            account,
            risk_day=timestamp.date(),
            risk_day_starting_equity=equity,
            updated_at=timestamp,
        )

    def _apply_loss_locks(
        self, connection: Connection, account: PaperAccount, equity: Decimal, timestamp: datetime
    ) -> None:
        daily_pnl = equity - account.risk_day_starting_equity
        if daily_pnl <= -account.daily_loss_limit:
            self.store.activate_lock(
                connection,
                account.account_id,
                PaperRiskLockType.DAILY_LOSS_LOCK,
                "utc_daily_loss_limit",
                timestamp,
            )
        if account.high_water_mark - equity >= account.max_drawdown:
            self.store.activate_lock(
                connection,
                account.account_id,
                PaperRiskLockType.DRAWDOWN_LOCK,
                "high_water_mark_drawdown_limit",
                timestamp,
            )


__all__ = ["PaperTradingEngine"]
