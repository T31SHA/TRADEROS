"""Immutable Phase 8 paper-trading contracts.

This module deliberately has no provider, socket, HTTP, or broker dependency.
It is the only quantity-bearing boundary after :class:`RiskDecision`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator

from traderos.backtesting.models import OrderSide, OrderType, TimeInForce
from traderos.data.instruments import Instrument
from traderos.data.time import require_utc
from traderos.risk.models import RiskAction, RiskDecision, RiskDecisionStatus


class PaperExecutionMode(StrEnum):
    """The only executable mode implemented by this package."""

    PAPER = "paper"


class PaperOrderStatus(StrEnum):
    CREATED = "created"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class PaperRiskLockType(StrEnum):
    DAILY_LOSS_LOCK = "daily_loss_lock"
    DRAWDOWN_LOCK = "drawdown_lock"
    EMERGENCY_LOCK = "emergency_lock"
    SYSTEM_HEALTH_LOCK = "system_health_lock"


_OPEN = frozenset(
    {
        PaperOrderStatus.ACCEPTED,
        PaperOrderStatus.PARTIALLY_FILLED,
        PaperOrderStatus.CANCEL_REQUESTED,
    }
)
_TRANSITIONS: dict[PaperOrderStatus, frozenset[PaperOrderStatus]] = {
    PaperOrderStatus.CREATED: frozenset({PaperOrderStatus.SUBMITTED, PaperOrderStatus.REJECTED}),
    PaperOrderStatus.SUBMITTED: frozenset({PaperOrderStatus.ACCEPTED, PaperOrderStatus.REJECTED}),
    PaperOrderStatus.ACCEPTED: frozenset(
        {
            PaperOrderStatus.PARTIALLY_FILLED,
            PaperOrderStatus.FILLED,
            PaperOrderStatus.CANCEL_REQUESTED,
            PaperOrderStatus.CANCELLED,
            PaperOrderStatus.REJECTED,
            PaperOrderStatus.EXPIRED,
        }
    ),
    PaperOrderStatus.PARTIALLY_FILLED: frozenset(
        {
            PaperOrderStatus.PARTIALLY_FILLED,
            PaperOrderStatus.FILLED,
            PaperOrderStatus.CANCEL_REQUESTED,
            PaperOrderStatus.CANCELLED,
            PaperOrderStatus.EXPIRED,
        }
    ),
    PaperOrderStatus.CANCEL_REQUESTED: frozenset(
        {PaperOrderStatus.CANCELLED, PaperOrderStatus.PARTIALLY_FILLED, PaperOrderStatus.FILLED}
    ),
    PaperOrderStatus.FILLED: frozenset(),
    PaperOrderStatus.CANCELLED: frozenset(),
    PaperOrderStatus.REJECTED: frozenset(),
    PaperOrderStatus.EXPIRED: frozenset(),
}


class PaperTradingError(ValueError):
    """A fail-closed paper-trading boundary error."""


def _finite_positive(value: Decimal, label: str) -> Decimal:
    if not value.is_finite() or value <= 0:
        raise PaperTradingError(f"{label} must be finite and positive")
    return value


def _utc(value: datetime) -> datetime:
    require_utc(value)
    return value


class PaperExecutionConfig(BaseModel):
    """Versioned deterministic execution/sizing assumptions.

    ``account_currency`` must match a submitted instrument settlement currency
    in v1; Phase 8 intentionally does not fabricate FX conversion or margin.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: PaperExecutionMode = PaperExecutionMode.PAPER
    decision_max_age: timedelta = timedelta(minutes=5)
    quote_max_age: timedelta = timedelta(minutes=1)
    quantity_increment: Decimal = Decimal("0.000001")
    minimum_quantity: Decimal = Decimal("0.000001")
    maximum_quantity: Decimal = Decimal("1000000")
    commission_per_unit: Decimal = Decimal("0")
    commission_rate: Decimal = Decimal("0")
    minimum_commission: Decimal = Decimal("0")
    slippage_absolute: Decimal = Decimal("0")
    max_fill_quantity: Decimal | None = None

    @field_validator("decision_max_age", "quote_max_age")
    @classmethod
    def positive_duration(cls, value: timedelta) -> timedelta:
        if value <= timedelta(0):
            raise ValueError("durations must be positive")
        return value

    @field_validator("quantity_increment", "minimum_quantity", "maximum_quantity")
    @classmethod
    def positive_decimal(cls, value: Decimal) -> Decimal:
        return _finite_positive(value, "quantity bound")

    @field_validator(
        "commission_per_unit", "commission_rate", "minimum_commission", "slippage_absolute"
    )
    @classmethod
    def nonnegative_decimal(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0:
            raise ValueError("cost values must be finite and non-negative")
        return value

    @field_validator("max_fill_quantity")
    @classmethod
    def valid_fill_quantity(cls, value: Decimal | None) -> Decimal | None:
        return None if value is None else _finite_positive(value, "max_fill_quantity")

    @property
    def configuration_id(self) -> str:
        payload = self.model_dump(mode="json")
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True)
class PaperQuote:
    instrument: Instrument
    timestamp: datetime
    bid: Decimal
    ask: Decimal

    def __post_init__(self) -> None:
        _utc(self.timestamp)
        _finite_positive(self.bid, "bid")
        _finite_positive(self.ask, "ask")
        if self.bid > self.ask:
            raise PaperTradingError("crossed paper quote")

    @property
    def midpoint(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")


@dataclass(frozen=True)
class PaperAccount:
    account_id: str
    account_currency: str
    starting_cash: Decimal
    cash: Decimal
    reserved_risk: Decimal
    risk_capacity: Decimal
    daily_loss_limit: Decimal
    max_drawdown: Decimal
    risk_policy_id: str
    risk_policy_configuration_id: str
    risk_day: date
    risk_day_starting_equity: Decimal
    high_water_mark: Decimal
    realized_pnl: Decimal
    fees: Decimal
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.account_id.strip() or not self.account_currency.strip():
            raise PaperTradingError("paper account identity must not be blank")
        for value in (
            self.starting_cash,
            self.cash,
            self.reserved_risk,
            self.risk_capacity,
            self.daily_loss_limit,
            self.max_drawdown,
            self.risk_day_starting_equity,
            self.high_water_mark,
            self.realized_pnl,
            self.fees,
        ):
            if not value.is_finite():
                raise PaperTradingError("paper account values must be finite")
        if (
            self.starting_cash <= 0
            or self.risk_capacity <= 0
            or self.daily_loss_limit <= 0
            or self.max_drawdown <= 0
            or self.high_water_mark <= 0
        ):
            raise PaperTradingError("paper account capital values must be positive")
        _utc(self.created_at)
        _utc(self.updated_at)


@dataclass(frozen=True)
class PaperPosition:
    account_id: str
    instrument: Instrument
    quantity: Decimal
    average_entry_price: Decimal
    market_price: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees: Decimal
    updated_at: datetime

    def __post_init__(self) -> None:
        _utc(self.updated_at)
        for value in (
            self.quantity,
            self.average_entry_price,
            self.market_price,
            self.realized_pnl,
            self.unrealized_pnl,
            self.fees,
        ):
            if not value.is_finite():
                raise PaperTradingError("position values must be finite")
        if self.average_entry_price < 0 or self.market_price < 0:
            raise PaperTradingError("position prices must be non-negative")


@dataclass(frozen=True)
class PaperOrder:
    order_id: str
    idempotency_key: str
    account_id: str
    instrument: Instrument
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    filled_quantity: Decimal
    average_fill_price: Decimal | None
    limit_price: Decimal | None
    stop_price: Decimal | None
    time_in_force: TimeInForce
    created_at: datetime
    submitted_at: datetime
    expires_at: datetime | None
    last_market_event_at: datetime | None
    risk_decision_id: str
    risk_policy_id: str
    risk_policy_configuration_id: str
    sizing_configuration_id: str
    source_intent_id: str
    authorization_action: RiskAction
    status: PaperOrderStatus
    rejection_reason: str | None = None

    def __post_init__(self) -> None:
        if not all(
            item.strip()
            for item in (
                self.order_id,
                self.idempotency_key,
                self.account_id,
                self.risk_decision_id,
                self.risk_policy_id,
                self.risk_policy_configuration_id,
                self.sizing_configuration_id,
                self.source_intent_id,
            )
        ):
            raise PaperTradingError("paper order identity must not be blank")
        _finite_positive(self.quantity, "quantity")
        if (
            not self.filled_quantity.is_finite()
            or not Decimal("0") <= self.filled_quantity <= self.quantity
        ):
            raise PaperTradingError("filled quantity is invalid")
        _utc(self.created_at)
        _utc(self.submitted_at)
        if self.expires_at is not None:
            _utc(self.expires_at)
        if self.last_market_event_at is not None:
            _utc(self.last_market_event_at)
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise PaperTradingError("limit order needs a limit price")
        if self.order_type is OrderType.STOP and self.stop_price is None:
            raise PaperTradingError("stop order needs a stop price")
        for price in (self.limit_price, self.stop_price, self.average_fill_price):
            if price is not None:
                _finite_positive(price, "order price")

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity

    @property
    def is_open(self) -> bool:
        return self.status in _OPEN

    def can_transition_to(self, new_status: PaperOrderStatus) -> bool:
        return new_status in _TRANSITIONS[self.status]


@dataclass(frozen=True)
class PaperFill:
    fill_id: str
    order_id: str
    account_id: str
    instrument: Instrument
    side: OrderSide
    quantity: Decimal
    price: Decimal
    reference_price: Decimal
    commission: Decimal
    timestamp: datetime

    def __post_init__(self) -> None:
        if not self.fill_id.strip() or not self.order_id.strip() or not self.account_id.strip():
            raise PaperTradingError("fill identity must not be blank")
        _finite_positive(self.quantity, "fill quantity")
        _finite_positive(self.price, "fill price")
        _finite_positive(self.reference_price, "reference price")
        if not self.commission.is_finite() or self.commission < 0:
            raise PaperTradingError("commission must be finite and non-negative")
        _utc(self.timestamp)


@dataclass(frozen=True)
class SizingResult:
    quantity: Decimal
    reserved_risk: Decimal
    reserved_cash: Decimal
    configuration_id: str


def quantity_for_authorization(
    *,
    decision: RiskDecision,
    quote: PaperQuote,
    account: PaperAccount,
    position: PaperPosition | None,
    reserved_risk: Decimal,
    reserved_cash: Decimal,
    config: PaperExecutionConfig,
    current_gross_exposure: Decimal = Decimal("0"),
) -> SizingResult:
    """Size only inside immutable Phase 7 bounds; this never mutates state."""

    if decision.status is not RiskDecisionStatus.APPROVE or decision.authorization is None:
        raise PaperTradingError("a rejected risk decision cannot be sized")
    if decision.instrument != quote.instrument:
        raise PaperTradingError("risk decision and quote instrument differ")
    if account.account_currency not in {
        quote.instrument.trading_currency,
        quote.instrument.quote_currency,
    }:
        raise PaperTradingError("Phase 8 paper sizing requires same-currency settlement")
    if not reserved_risk.is_finite() or reserved_risk < 0:
        raise PaperTradingError("reserved risk is invalid")
    if not reserved_cash.is_finite() or reserved_cash < 0:
        raise PaperTradingError("reserved cash is invalid")
    if not current_gross_exposure.is_finite() or current_gross_exposure < 0:
        raise PaperTradingError("current gross exposure is invalid")
    price = quote.ask if decision.direction.value == "long" else quote.bid
    _finite_positive(price, "sizing price")
    authorization = decision.authorization
    if authorization.action is RiskAction.NEW_OR_INCREASE:
        available = min(
            authorization.max_new_notional,
            account.risk_capacity - reserved_risk - current_gross_exposure,
        )
        if available <= 0:
            raise PaperTradingError("no durable risk capacity remains")
        if decision.direction.value == "long":
            available = min(available, account.cash - reserved_cash)
        raw_quantity = available / price
        new_risk = available
    else:
        current = Decimal("0") if position is None else abs(position.quantity)
        raw_quantity = min(current, authorization.max_reduction_notional / price)
        new_risk = Decimal("0")
        if raw_quantity <= 0:
            raise PaperTradingError("reduction-only authorization has no reducible position")
    quantity = (raw_quantity / config.quantity_increment).to_integral_value(rounding=ROUND_DOWN)
    quantity *= config.quantity_increment
    if authorization.action is RiskAction.NEW_OR_INCREASE and quantity < config.minimum_quantity:
        raise PaperTradingError("no durable risk capacity remains")
    if quantity < config.minimum_quantity or quantity > config.maximum_quantity:
        raise PaperTradingError("sized quantity violates configured bounds")
    notional = quantity * price
    if (
        authorization.action is RiskAction.NEW_OR_INCREASE
        and notional > authorization.max_new_notional
    ):
        raise PaperTradingError("sizing would exceed risk authorization")
    reserved_cash = notional if decision.direction.value == "long" else Decimal("0")
    return SizingResult(
        quantity, notional if new_risk else Decimal("0"), reserved_cash, config.configuration_id
    )


__all__ = [
    "PaperAccount",
    "PaperExecutionConfig",
    "PaperExecutionMode",
    "PaperFill",
    "PaperOrder",
    "PaperOrderStatus",
    "PaperPosition",
    "PaperQuote",
    "PaperRiskLockType",
    "PaperTradingError",
    "SizingResult",
    "quantity_for_authorization",
]
