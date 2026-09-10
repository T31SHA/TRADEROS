"""Contracts and immutable configuration for deterministic backtests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from traderos.backtesting.errors import InvalidOrderError, InvalidOrderTransition
from traderos.data.bars import MarketBar
from traderos.data.instruments import Instrument
from traderos.data.lineage import AdjustmentPolicy
from traderos.data.time import require_utc
from traderos.data.timeframes import Timeframe


def _finite_decimal(value: Decimal, *, positive: bool = False) -> Decimal:
    if not value.is_finite() or (positive and value <= 0):
        raise ValueError("value must be finite" + (" and positive" if positive else ""))
    return value


class OrderSide(StrEnum):
    """Direction of an order or fill."""

    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    """Initial order types supported by the simulator."""

    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class TimeInForce(StrEnum):
    """Supported order lifetime policies."""

    GTC = "gtc"
    DAY = "day"
    IOC = "ioc"


class OrderStatus(StrEnum):
    """Explicit order lifecycle states."""

    CREATED = "created"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ExecutionPolicy(StrEnum):
    """When orders submitted at a completed bar may first execute."""

    NEXT_BAR_OPEN = "next_bar_open"


class IntrabarAmbiguityPolicy(StrEnum):
    """How multiple conditional orders triggered by one OHLC bar are handled."""

    REJECT = "reject"
    FIRST_ORDER_ID = "first_order_id"


class MissingBarPolicy(StrEnum):
    """Whether an injected calendar should reject missing expected bars."""

    ALLOW = "allow"
    REJECT = "reject"


@dataclass
class Order:
    """Mutable order lifecycle record owned by the backtest order book."""

    order_id: str
    instrument: Instrument
    side: OrderSide
    quantity: Decimal
    order_type: OrderType = OrderType.MARKET
    submitted_timestamp: datetime | None = None
    created_timestamp: datetime | None = None
    time_in_force: TimeInForce = TimeInForce.GTC
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    strategy_id: str = ""
    strategy_version: str = ""
    parent_order_id: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    status: OrderStatus = OrderStatus.CREATED
    filled_quantity: Decimal = Decimal("0")
    rejection_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.order_id.strip():
            raise InvalidOrderError("order_id must not be blank")
        if not self.quantity.is_finite() or self.quantity <= 0:
            raise InvalidOrderError("order quantity must be finite and positive")
        if not self.filled_quantity.is_finite() or self.filled_quantity < 0:
            raise InvalidOrderError("filled quantity must be finite and non-negative")
        if self.filled_quantity > self.quantity:
            raise InvalidOrderError("filled quantity cannot exceed order quantity")
        if self.order_type is OrderType.LIMIT:
            if (
                self.limit_price is None
                or not self.limit_price.is_finite()
                or self.limit_price <= 0
            ):
                raise InvalidOrderError("limit orders require a positive limit_price")
        elif self.limit_price is not None:
            raise InvalidOrderError("only limit orders may specify limit_price")
        if self.order_type is OrderType.STOP:
            if self.stop_price is None or not self.stop_price.is_finite() or self.stop_price <= 0:
                raise InvalidOrderError("stop orders require a positive stop_price")
        elif self.stop_price is not None:
            raise InvalidOrderError("only stop orders may specify stop_price")
        for timestamp in (self.submitted_timestamp, self.created_timestamp):
            if timestamp is not None:
                require_utc(timestamp)

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity

    @property
    def is_open(self) -> bool:
        return self.status in {
            OrderStatus.SUBMITTED,
            OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED,
        }

    def transition(self, new_status: OrderStatus, *, reason: str | None = None) -> None:
        valid: dict[OrderStatus, frozenset[OrderStatus]] = {
            OrderStatus.CREATED: frozenset({OrderStatus.SUBMITTED, OrderStatus.REJECTED}),
            OrderStatus.SUBMITTED: frozenset(
                {OrderStatus.ACCEPTED, OrderStatus.REJECTED, OrderStatus.CANCELLED}
            ),
            OrderStatus.ACCEPTED: frozenset(
                {
                    OrderStatus.PARTIALLY_FILLED,
                    OrderStatus.FILLED,
                    OrderStatus.REJECTED,
                    OrderStatus.CANCELLED,
                    OrderStatus.EXPIRED,
                }
            ),
            OrderStatus.PARTIALLY_FILLED: frozenset(
                {
                    OrderStatus.PARTIALLY_FILLED,
                    OrderStatus.FILLED,
                    OrderStatus.CANCELLED,
                    OrderStatus.EXPIRED,
                }
            ),
            OrderStatus.FILLED: frozenset(),
            OrderStatus.CANCELLED: frozenset(),
            OrderStatus.REJECTED: frozenset(),
            OrderStatus.EXPIRED: frozenset(),
        }
        if new_status not in valid[self.status]:
            raise InvalidOrderTransition(f"{self.status.value} → {new_status.value} is invalid")
        self.status = new_status
        if reason is not None:
            self.rejection_reason = reason

    def record_fill(self, quantity: Decimal) -> None:
        if quantity <= 0 or quantity > self.remaining_quantity:
            raise InvalidOrderError("fill quantity must be positive and within remaining quantity")
        self.filled_quantity += quantity
        self.transition(
            OrderStatus.FILLED if self.remaining_quantity == 0 else OrderStatus.PARTIALLY_FILLED
        )


@dataclass(frozen=True)
class Fill:
    """Auditable execution result for one order slice."""

    fill_id: str
    order_id: str
    instrument: Instrument
    side: OrderSide
    quantity: Decimal
    price: Decimal
    reference_price: Decimal
    slippage: Decimal
    commission: Decimal
    commission_currency: str
    timestamp: datetime
    strategy_id: str
    strategy_version: str

    def __post_init__(self) -> None:
        if not self.quantity.is_finite() or self.quantity <= 0:
            raise ValueError("fill quantity must be finite and positive")
        if not self.price.is_finite() or self.price <= 0:
            raise ValueError("fill price must be finite and positive")
        if self.slippage < 0 or self.commission < 0:
            raise ValueError("slippage and commission must be non-negative")
        require_utc(self.timestamp)

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.price


@dataclass
class Position:
    """Signed position in instrument units."""

    instrument: Instrument
    quantity: Decimal = Decimal("0")
    average_entry_price: Decimal = Decimal("0")
    market_price: Decimal = Decimal("0")
    market_value: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    entry_fees: Decimal = Decimal("0")
    opened_timestamp: datetime | None = None


@dataclass(frozen=True)
class PositionSnapshot:
    """Immutable position view supplied to strategies and reports."""

    symbol: str
    quantity: Decimal
    average_entry_price: Decimal
    market_price: Decimal
    market_value: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees: Decimal


@dataclass(frozen=True)
class LedgerEntry:
    """One fill-to-accounting reconciliation record."""

    fill: Fill
    cash_before: Decimal
    cash_after: Decimal
    quantity_before: Decimal
    quantity_after: Decimal
    realized_pnl: Decimal
    fees: Decimal
    equity_after: Decimal


@dataclass(frozen=True)
class TradeRecord:
    """A closed position slice used for trade-level analytics."""

    trade_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    entry_timestamp: datetime
    exit_timestamp: datetime
    entry_price: Decimal
    exit_price: Decimal
    gross_pnl: Decimal
    fees: Decimal
    net_pnl: Decimal

    @property
    def holding_period(self) -> timedelta:
        return self.exit_timestamp - self.entry_timestamp


@dataclass(frozen=True)
class EquitySnapshot:
    """Portfolio state at a market-event decision timestamp."""

    timestamp: datetime
    cash: Decimal
    gross_market_value: Decimal
    net_market_value: Decimal
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees: Decimal
    gross_exposure: Decimal
    positions: tuple[PositionSnapshot, ...]


@dataclass(frozen=True)
class Commission:
    amount: Decimal
    currency: str


class CostConfig(BaseModel):
    """Explicit deterministic transaction-cost parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    per_unit: Decimal = Decimal("0")
    rate: Decimal = Decimal("0")
    minimum: Decimal = Decimal("0")

    @field_validator("per_unit", "rate", "minimum")
    @classmethod
    def nonnegative(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0:
            raise ValueError("cost parameters must be finite and non-negative")
        return value


class SpreadConfig(BaseModel):
    """Fallback absolute spread used when a bar has no bid/ask quote."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fallback_absolute: Decimal = Decimal("0")

    @field_validator("fallback_absolute")
    @classmethod
    def nonnegative(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0:
            raise ValueError("spread must be finite and non-negative")
        return value


class SlippageConfig(BaseModel):
    """Deterministic absolute adverse slippage per unit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    absolute: Decimal = Decimal("0")

    @field_validator("absolute")
    @classmethod
    def nonnegative(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0:
            raise ValueError("slippage must be finite and non-negative")
        return value


class InitialPosition(BaseModel):
    """Optional position seeded at the first available bar open."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(min_length=1)
    quantity: Decimal

    @field_validator("quantity")
    @classmethod
    def finite_quantity(cls, value: Decimal) -> Decimal:
        return _finite_decimal(value)


class BacktestConfig(BaseModel):
    """All assumptions needed to identify and replay a backtest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_version: str = Field(min_length=1)
    instrument_symbols: tuple[str, ...] = Field(min_length=1)
    timeframe: Timeframe
    start: datetime
    end: datetime
    starting_cash: Decimal = Field(gt=0)
    account_currency: str = Field(min_length=1)
    strategy_id: str = Field(min_length=1)
    strategy_version: str = Field(min_length=1)
    feature_versions: tuple[tuple[str, int], ...] = ()
    adjustment_policy: AdjustmentPolicy = AdjustmentPolicy.RAW
    commission: CostConfig = CostConfig()
    spread: SpreadConfig = SpreadConfig()
    slippage: SlippageConfig = SlippageConfig()
    execution_policy: ExecutionPolicy = ExecutionPolicy.NEXT_BAR_OPEN
    ambiguity_policy: IntrabarAmbiguityPolicy = IntrabarAmbiguityPolicy.REJECT
    missing_bar_policy: MissingBarPolicy = MissingBarPolicy.ALLOW
    max_fill_quantity: Decimal | None = None
    initial_positions: tuple[InitialPosition, ...] = ()
    annualization_factor: int | None = None
    risk_free_rate: float = 0.0
    calendar_id: str | None = None

    @field_validator("start", "end")
    @classmethod
    def utc_timestamps(cls, value: datetime) -> datetime:
        require_utc(value)
        return value

    @field_validator("instrument_symbols")
    @classmethod
    def unique_symbols(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not symbol.strip() for symbol in values) or len(set(values)) != len(values):
            raise ValueError("instrument_symbols must be unique and non-blank")
        return values

    @field_validator("annualization_factor")
    @classmethod
    def valid_annualization(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("annualization_factor must be positive")
        return value

    @field_validator("max_fill_quantity")
    @classmethod
    def valid_liquidity(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and (not value.is_finite() or value <= 0):
            raise ValueError("max_fill_quantity must be positive when provided")
        return value

    @model_validator(mode="after")
    def validate_range_and_positions(self) -> BacktestConfig:
        if self.start >= self.end:
            raise ValueError("backtest start must precede end")
        symbols = set(self.instrument_symbols)
        if any(position.symbol not in symbols for position in self.initial_positions):
            raise ValueError("initial positions must belong to instrument_symbols")
        if len({position.symbol for position in self.initial_positions}) != len(
            self.initial_positions
        ):
            raise ValueError("initial_positions must contain each symbol at most once")
        return self

    @property
    def experiment_id(self) -> str:
        payload = self.model_dump(mode="json")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class StrategyContext:
    """Information visible to a strategy at one completed-bar decision."""

    event_timestamp: datetime
    bar: MarketBar
    features: Mapping[tuple[str, int], float | None]
    cash: Decimal
    equity: Decimal
    positions: tuple[PositionSnapshot, ...]

    def __post_init__(self) -> None:
        require_utc(self.event_timestamp)
        if self.event_timestamp < self.bar.timestamp + self.bar.timeframe.duration:
            raise ValueError("strategy context cannot precede bar completion")


@dataclass(frozen=True)
class BacktestResult:
    """Complete deterministic replay output."""

    experiment_id: str
    config: BacktestConfig
    event_count: int
    decision_count: int
    orders: tuple[Order, ...]
    fills: tuple[Fill, ...]
    ledger: tuple[LedgerEntry, ...]
    trades: tuple[TradeRecord, ...]
    equity_curve: tuple[EquitySnapshot, ...]
    metrics: PerformanceMetrics
    rejected_orders: tuple[str, ...] = ()
    open_orders: tuple[Order, ...] = ()


@dataclass(frozen=True)
class PerformanceMetrics:
    """Deterministic performance and trade statistics."""

    total_return: float | None
    cagr: float | None
    annualized_volatility: float | None
    maximum_drawdown: float | None
    maximum_drawdown_duration: timedelta | None
    recovery_time: timedelta | None
    sharpe_ratio: float | None
    sortino_ratio: float | None
    calmar_ratio: float | None
    periodic_returns: tuple[float, ...]
    trade_count: int
    winning_trades: int
    losing_trades: int
    win_rate: float | None
    average_win: float | None
    average_loss: float | None
    profit_factor: float | None
    expectancy: float | None
    largest_win: float | None
    largest_loss: float | None
    average_holding_period: timedelta | None
    turnover: float
    total_fees: Decimal


__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "Commission",
    "CostConfig",
    "EquitySnapshot",
    "ExecutionPolicy",
    "Fill",
    "InitialPosition",
    "IntrabarAmbiguityPolicy",
    "LedgerEntry",
    "MissingBarPolicy",
    "Order",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "PerformanceMetrics",
    "Position",
    "PositionSnapshot",
    "SlippageConfig",
    "SpreadConfig",
    "StrategyContext",
    "TimeInForce",
    "TradeRecord",
]
