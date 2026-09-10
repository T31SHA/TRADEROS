"""Explicit errors for the deterministic strategy boundary."""

from traderos.core.errors import TraderosError


class StrategyError(TraderosError):
    """Base class for strategy-layer failures."""


class StrategyConfigurationError(StrategyError):
    """Raised for invalid strategy metadata or parameters."""


class StrategyCausalityError(StrategyError):
    """Raised when a strategy context violates its information boundary."""


class StrategyRegistryError(StrategyError):
    """Raised when registry identity or scope rules are violated."""


class MissingFeatureError(StrategyCausalityError):
    """Raised when a declared feature is absent from a causal context."""


__all__ = [
    "MissingFeatureError",
    "StrategyCausalityError",
    "StrategyConfigurationError",
    "StrategyError",
    "StrategyRegistryError",
]
