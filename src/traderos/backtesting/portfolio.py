"""Multi-asset cash, position, P&L, and equity accounting."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from traderos.backtesting.errors import AccountingError, InsufficientCash
from traderos.backtesting.models import (
    EquitySnapshot,
    Fill,
    LedgerEntry,
    OrderSide,
    Position,
    PositionSnapshot,
    TradeRecord,
)
from traderos.data.bars import MarketBar
from traderos.data.instruments import Instrument
from traderos.data.time import require_utc


class CurrencyConverter(Protocol):
    """Convert settlement amounts into the configured account currency."""

    def convert(
        self,
        amount: Decimal,
        from_currency: str,
        to_currency: str,
        timestamp: datetime,
    ) -> Decimal:
        """Convert an amount using explicitly configured rates."""


@dataclass(frozen=True)
class IdentityCurrencyConverter:
    """Safe converter for same-currency accounts only."""

    def convert(
        self,
        amount: Decimal,
        from_currency: str,
        to_currency: str,
        timestamp: datetime,
    ) -> Decimal:
        del timestamp
        if from_currency != to_currency:
            raise AccountingError(f"no conversion configured from {from_currency} to {to_currency}")
        return amount


@dataclass(frozen=True)
class StaticCurrencyConverter:
    """Deterministic explicitly supplied currency conversion table."""

    rates: Mapping[tuple[str, str], Decimal]

    def convert(
        self,
        amount: Decimal,
        from_currency: str,
        to_currency: str,
        timestamp: datetime,
    ) -> Decimal:
        del timestamp
        if from_currency == to_currency:
            return amount
        direct = self.rates.get((from_currency, to_currency))
        if direct is not None:
            return amount * direct
        inverse = self.rates.get((to_currency, from_currency))
        if inverse is not None and inverse != 0:
            return amount / inverse
        raise AccountingError(f"no conversion configured from {from_currency} to {to_currency}")


def _settlement_currency(instrument: Instrument) -> str:
    currency = instrument.quote_currency or instrument.trading_currency
    if currency is None:
        raise AccountingError(
            f"settlement currency is unavailable for {instrument.canonical_symbol}"
        )
    return currency


class Portfolio:
    """Mutable account state with all accounting formulas centralized."""

    def __init__(
        self,
        *,
        starting_cash: Decimal,
        account_currency: str,
        converter: CurrencyConverter | None = None,
    ) -> None:
        if not starting_cash.is_finite() or starting_cash <= 0:
            raise AccountingError("starting cash must be finite and positive")
        self.starting_cash = starting_cash
        self.account_currency = account_currency
        self.cash = starting_cash
        self.converter = converter or IdentityCurrencyConverter()
        self._positions: dict[str, Position] = {}
        self._trades: list[TradeRecord] = []
        self._trade_sequence = 0
        self.realized_pnl = Decimal("0")
        self.fees = Decimal("0")

    def seed_position(
        self, instrument: Instrument, quantity: Decimal, price: Decimal, timestamp: datetime
    ) -> None:
        """Seed an initial position at the first available bar open."""

        if quantity == 0 or not quantity.is_finite() or not price.is_finite() or price <= 0:
            raise AccountingError("initial position quantity and price are invalid")
        require_utc(timestamp)
        symbol = instrument.canonical_symbol
        if symbol in self._positions:
            raise AccountingError(f"initial position already exists for {symbol}")
        position = Position(
            instrument=instrument,
            quantity=quantity,
            average_entry_price=price,
            market_price=price,
            opened_timestamp=timestamp,
        )
        settlement = _settlement_currency(instrument)
        cash_change = self.converter.convert(
            -quantity * price, settlement, self.account_currency, timestamp
        )
        if self.cash + cash_change < 0:
            raise InsufficientCash(
                f"initial position would reduce cash below zero in {self.account_currency}"
            )
        self._positions[symbol] = position
        self.cash += cash_change
        self._mark_position(position, timestamp)

    def apply_fill(self, fill: Fill) -> tuple[LedgerEntry, tuple[TradeRecord, ...]]:
        """Apply one fill exactly once and return ledger/trade records."""

        symbol = fill.instrument.canonical_symbol
        position = self._positions.get(symbol)
        if position is None:
            position = Position(instrument=fill.instrument)
            self._positions[symbol] = position
        before_cash = self.cash
        before_quantity = position.quantity
        before_entry_fees = position.entry_fees
        signed_quantity = fill.quantity if fill.side is OrderSide.BUY else -fill.quantity
        settlement = _settlement_currency(fill.instrument)
        notional_cash = self.converter.convert(
            -signed_quantity * fill.price,
            settlement,
            self.account_currency,
            fill.timestamp,
        )
        fee_account = self.converter.convert(
            -fill.commission,
            fill.commission_currency,
            self.account_currency,
            fill.timestamp,
        )
        projected_cash = before_cash + notional_cash + fee_account
        if projected_cash < 0:
            raise InsufficientCash(
                f"fill {fill.fill_id} would reduce cash below zero in {self.account_currency}"
            )
        closing_trades = self._update_position(position, fill, signed_quantity, settlement)
        self.cash = projected_cash
        fee_amount = -fee_account
        self.fees += fee_amount
        position.fees += fee_amount
        adjusted_trades: list[TradeRecord] = []
        closed_quantity = sum((trade.quantity for trade in closing_trades), Decimal("0"))
        closed_entry_fees = Decimal("0")
        if before_quantity != 0 and closed_quantity != 0:
            closed_entry_fees = before_entry_fees * closed_quantity / abs(before_quantity)
        for trade in closing_trades:
            exit_fee_share = fee_amount * trade.quantity / fill.quantity
            entry_fee_share = closed_entry_fees * trade.quantity / closed_quantity
            fee_share = entry_fee_share + exit_fee_share
            adjusted = replace(trade, fees=fee_share, net_pnl=trade.gross_pnl - fee_share)
            self._trades[-1] = adjusted
            adjusted_trades.append(adjusted)
        if before_quantity == 0 or before_quantity * signed_quantity > 0:
            position.entry_fees = before_entry_fees + fee_amount
        elif position.quantity == 0:
            position.entry_fees = Decimal("0")
        elif before_quantity * position.quantity > 0:
            position.entry_fees = before_entry_fees - closed_entry_fees
        else:
            opened_quantity = abs(position.quantity)
            position.entry_fees = fee_amount * opened_quantity / fill.quantity
        position.market_price = fill.price
        self._mark_position(position, fill.timestamp)
        self.realized_pnl += sum((trade.gross_pnl for trade in adjusted_trades), Decimal("0"))
        equity = self.equity(fill.timestamp)
        return (
            LedgerEntry(
                fill=fill,
                cash_before=before_cash,
                cash_after=self.cash,
                quantity_before=before_quantity,
                quantity_after=position.quantity,
                realized_pnl=sum((trade.gross_pnl for trade in adjusted_trades), Decimal("0")),
                fees=-fee_account,
                equity_after=equity,
            ),
            tuple(adjusted_trades),
        )

    def mark_to_market(self, bar: MarketBar, timestamp: datetime) -> None:
        require_utc(timestamp)
        position = self._positions.get(bar.symbol)
        if position is not None:
            position.market_price = bar.close
            self._mark_position(position, timestamp)

    def snapshot(self, timestamp: datetime) -> EquitySnapshot:
        require_utc(timestamp)
        snapshots = tuple(
            PositionSnapshot(
                symbol=position.instrument.canonical_symbol,
                quantity=position.quantity,
                average_entry_price=position.average_entry_price,
                market_price=position.market_price,
                market_value=position.market_value,
                realized_pnl=position.realized_pnl,
                unrealized_pnl=position.unrealized_pnl,
                fees=position.fees,
            )
            for position in sorted(
                self._positions.values(), key=lambda item: item.instrument.canonical_symbol
            )
            if position.quantity != 0
        )
        gross = sum((abs(item.market_value) for item in snapshots), Decimal("0"))
        net = sum((item.market_value for item in snapshots), Decimal("0"))
        unrealized = sum((item.unrealized_pnl for item in snapshots), Decimal("0"))
        return EquitySnapshot(
            timestamp=timestamp,
            cash=self.cash,
            gross_market_value=gross,
            net_market_value=net,
            equity=self.cash + net,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=unrealized,
            fees=self.fees,
            gross_exposure=gross,
            positions=snapshots,
        )

    def equity(self, timestamp: datetime) -> Decimal:
        return self.snapshot(timestamp).equity

    @property
    def trades(self) -> tuple[TradeRecord, ...]:
        return tuple(self._trades)

    def convert_to_account(
        self, amount: Decimal, currency: str | None, timestamp: datetime
    ) -> Decimal:
        if currency is None:
            raise AccountingError("currency is required for account conversion")
        return self.converter.convert(amount, currency, self.account_currency, timestamp)

    def _update_position(
        self, position: Position, fill: Fill, signed_quantity: Decimal, settlement: str
    ) -> list[TradeRecord]:
        del settlement
        old_quantity = position.quantity
        closing: list[TradeRecord] = []
        if old_quantity == 0 or old_quantity * signed_quantity > 0:
            total = abs(old_quantity) + abs(signed_quantity)
            if old_quantity == 0:
                position.average_entry_price = fill.price
                position.opened_timestamp = fill.timestamp
            else:
                position.average_entry_price = (
                    position.average_entry_price * abs(old_quantity)
                    + fill.price * abs(signed_quantity)
                ) / total
            position.quantity += signed_quantity
            return closing

        close_quantity = min(abs(old_quantity), abs(signed_quantity))
        direction = Decimal("1") if old_quantity > 0 else Decimal("-1")
        gross_quote = (fill.price - position.average_entry_price) * close_quantity * direction
        entry_timestamp = position.opened_timestamp or fill.timestamp
        side = OrderSide.BUY if old_quantity > 0 else OrderSide.SELL
        gross_account = self.converter.convert(
            gross_quote,
            _settlement_currency(fill.instrument),
            self.account_currency,
            fill.timestamp,
        )
        self._trade_sequence += 1
        trade = TradeRecord(
            trade_id=f"trade-{self._trade_sequence}",
            symbol=fill.instrument.canonical_symbol,
            side=side,
            quantity=close_quantity,
            entry_timestamp=entry_timestamp,
            exit_timestamp=fill.timestamp,
            entry_price=position.average_entry_price,
            exit_price=fill.price,
            gross_pnl=gross_account,
            fees=Decimal("0"),
            net_pnl=gross_account,
        )
        closing.append(trade)
        position.realized_pnl += gross_account
        self._trades.append(trade)
        remaining = old_quantity + signed_quantity
        if remaining == 0:
            position.quantity = Decimal("0")
            position.average_entry_price = Decimal("0")
            position.opened_timestamp = None
        elif old_quantity * remaining > 0:
            position.quantity = remaining
        else:
            position.quantity = remaining
            position.average_entry_price = fill.price
            position.opened_timestamp = fill.timestamp
        return closing

    def _mark_position(self, position: Position, timestamp: datetime) -> None:
        if position.quantity == 0:
            position.market_value = Decimal("0")
            position.unrealized_pnl = Decimal("0")
            return
        settlement = _settlement_currency(position.instrument)
        position.market_value = self.converter.convert(
            position.quantity * position.market_price,
            settlement,
            self.account_currency,
            timestamp,
        )
        quote_pnl = (position.market_price - position.average_entry_price) * position.quantity
        position.unrealized_pnl = self.converter.convert(
            quote_pnl,
            settlement,
            self.account_currency,
            timestamp,
        )


__all__ = [
    "CurrencyConverter",
    "IdentityCurrencyConverter",
    "Portfolio",
    "StaticCurrencyConverter",
]
