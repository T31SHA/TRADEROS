"""Explicit failures for the offline backtesting domain."""

from traderos.core.errors import TraderosError


class BacktestError(TraderosError):
    """Base class for expected backtest failures."""


class BacktestConfigurationError(BacktestError):
    """Raised when a backtest configuration is unsafe or incomplete."""


class InvalidMarketDataError(BacktestError):
    """Raised when the input event stream violates its data contract."""


class InvalidOrderError(BacktestError):
    """Raised when an order violates its type or quantity contract."""


class InvalidOrderTransition(BacktestError):
    """Raised for an impossible order lifecycle transition."""


class OrderTimingError(BacktestError):
    """Raised when an order attempts to execute before its eligibility time."""


class ExecutionPolicyError(BacktestError):
    """Raised when execution assumptions cannot produce a safe fill."""


class AccountingError(BacktestError):
    """Raised when portfolio accounting cannot be performed safely."""


class InsufficientCash(BacktestError):
    """Raised when a fill would make cash negative under the configured model."""


class CausalityViolation(BacktestError):
    """Raised when future market or feature information reaches a decision."""


__all__ = [
    "AccountingError",
    "BacktestConfigurationError",
    "BacktestError",
    "CausalityViolation",
    "ExecutionPolicyError",
    "InsufficientCash",
    "InvalidMarketDataError",
    "InvalidOrderError",
    "InvalidOrderTransition",
    "OrderTimingError",
]
