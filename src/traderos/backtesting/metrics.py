"""Performance analytics derived only from recorded portfolio state."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from math import sqrt
from statistics import mean
from typing import Protocol

from traderos.backtesting.models import EquitySnapshot, PerformanceMetrics, TradeRecord
from traderos.data.bars import MarketBar


def _safe_divide(numerator: float, denominator: float) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _drawdown_stats(
    curve: Sequence[EquitySnapshot],
) -> tuple[float | None, timedelta | None, timedelta | None]:
    if not curve:
        return None, None, None
    peak = float(curve[0].equity)
    peak_time = curve[0].timestamp
    active_trough: float | None = None
    active_trough_time: datetime | None = None
    segments: list[tuple[float, datetime, float, datetime, datetime | None]] = []

    for snapshot in curve[1:]:
        equity = float(snapshot.equity)
        if equity < peak:
            if active_trough is None or equity < active_trough:
                active_trough = equity
                active_trough_time = snapshot.timestamp
            continue
        if active_trough is not None and active_trough_time is not None:
            segments.append(
                (peak, peak_time, active_trough, active_trough_time, snapshot.timestamp)
            )
        peak = equity
        peak_time = snapshot.timestamp
        active_trough = None
        active_trough_time = None

    if active_trough is not None and active_trough_time is not None:
        segments.append((peak, peak_time, active_trough, active_trough_time, None))
    if not segments:
        return 0.0, None, None

    worst = min(segments, key=lambda item: item[2] / item[0] - 1.0 if item[0] else 0.0)
    max_drawdown = worst[2] / worst[0] - 1.0 if worst[0] else 0.0
    max_duration = max(
        (
            (recovery or curve[-1].timestamp) - segment_peak_time
            for _, segment_peak_time, _, _, recovery in segments
        ),
        default=timedelta(0),
    )
    recovery_time = None
    if worst[4] is not None:
        recovery_time = worst[4] - worst[3]
    return max_drawdown, max_duration or None, recovery_time


def calculate_metrics(
    curve: Sequence[EquitySnapshot],
    trades: Sequence[TradeRecord],
    *,
    starting_equity: Decimal,
    annualization_factor: int | None,
    risk_free_rate: float = 0.0,
    turnover_notional: float = 0.0,
    total_fees: Decimal,
) -> PerformanceMetrics:
    """Calculate metrics without consulting bars or future external data."""

    if not curve:
        return PerformanceMetrics(
            total_return=None,
            cagr=None,
            annualized_volatility=None,
            maximum_drawdown=None,
            maximum_drawdown_duration=None,
            recovery_time=None,
            sharpe_ratio=None,
            sortino_ratio=None,
            calmar_ratio=None,
            periodic_returns=(),
            trade_count=0,
            winning_trades=0,
            losing_trades=0,
            win_rate=None,
            average_win=None,
            average_loss=None,
            profit_factor=None,
            expectancy=None,
            largest_win=None,
            largest_loss=None,
            average_holding_period=None,
            turnover=0.0,
            total_fees=total_fees,
        )

    final_equity = float(curve[-1].equity)
    starting = float(starting_equity)
    total_return = _safe_divide(final_equity, starting)
    total_return = None if total_return is None else total_return - 1.0
    elapsed_days = (curve[-1].timestamp - curve[0].timestamp).total_seconds() / 86400
    cagr = None
    if total_return is not None and final_equity > 0 and starting > 0 and elapsed_days > 0:
        cagr = (final_equity / starting) ** (365.2425 / elapsed_days) - 1.0

    periodic_returns = tuple(
        current / previous - 1.0
        for previous, current in zip(
            (float(snapshot.equity) for snapshot in curve[:-1]),
            (float(snapshot.equity) for snapshot in curve[1:]),
            strict=True,
        )
        if previous != 0
    )
    annualized_volatility = None
    sharpe = None
    sortino = None
    if annualization_factor is not None and periodic_returns:
        avg = mean(periodic_returns)
        variance = mean((value - avg) ** 2 for value in periodic_returns)
        volatility = sqrt(variance)
        annualized_volatility = volatility * sqrt(annualization_factor)
        excess = avg - risk_free_rate / annualization_factor
        sharpe = _safe_divide(excess, volatility)
        if sharpe is not None:
            sharpe *= sqrt(annualization_factor)
        downside = [
            min(value - risk_free_rate / annualization_factor, 0.0) for value in periodic_returns
        ]
        downside_deviation = sqrt(mean(value**2 for value in downside))
        sortino = _safe_divide(excess, downside_deviation)
        if sortino is not None:
            sortino *= sqrt(annualization_factor)

    maximum_drawdown, drawdown_duration, recovery_time = _drawdown_stats(curve)
    calmar = None
    if cagr is not None and maximum_drawdown is not None and maximum_drawdown != 0.0:
        calmar = cagr / abs(maximum_drawdown)

    pnls = [float(trade.net_pnl) for trade in trades]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    average_holding = None
    if trades:
        average_holding = sum((trade.holding_period for trade in trades), timedelta(0)) / len(
            trades
        )
    return PerformanceMetrics(
        total_return=total_return,
        cagr=cagr,
        annualized_volatility=annualized_volatility,
        maximum_drawdown=maximum_drawdown,
        maximum_drawdown_duration=drawdown_duration,
        recovery_time=recovery_time,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        calmar_ratio=calmar,
        periodic_returns=periodic_returns,
        trade_count=len(trades),
        winning_trades=len(wins),
        losing_trades=len(losses),
        win_rate=_safe_divide(len(wins), len(trades)),
        average_win=mean(wins) if wins else None,
        average_loss=mean(losses) if losses else None,
        profit_factor=_safe_divide(gross_wins, gross_losses),
        expectancy=mean(pnls) if pnls else None,
        largest_win=max(wins) if wins else None,
        largest_loss=min(losses) if losses else None,
        average_holding_period=average_holding,
        turnover=turnover_notional / starting if starting else 0.0,
        total_fees=total_fees,
    )


class Benchmark(Protocol):
    """Benchmark calculation boundary."""

    def equity_curve(self, bars: Sequence[MarketBar], starting_cash: Decimal) -> tuple[float, ...]:
        """Return benchmark equity at each supplied event."""


class BuyAndHoldBenchmark:
    """Simple first-bar-open buy-and-hold benchmark for one instrument."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol

    def equity_curve(self, bars: Sequence[MarketBar], starting_cash: Decimal) -> tuple[float, ...]:
        selected = [bar for bar in bars if bar.symbol == self.symbol]
        if not selected:
            return ()
        quantity = starting_cash / selected[0].open
        return tuple(float(quantity * bar.close) for bar in selected)


__all__ = ["Benchmark", "BuyAndHoldBenchmark", "calculate_metrics"]
