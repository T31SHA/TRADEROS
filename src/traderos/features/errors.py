"""Errors raised by the deterministic feature engine."""

from traderos.core.errors import TraderosError


class FeatureError(TraderosError):
    """Base class for feature-domain failures."""


class FeatureConfigurationError(FeatureError):
    """Raised for an invalid feature definition or request."""


class FeatureValidationError(FeatureError):
    """Raised when input bars or computed observations are invalid."""


class FeatureComputationError(FeatureError):
    """Raised when a feature cannot be computed safely."""


class CausalityViolationError(FeatureError):
    """Raised when a feature's availability contract is violated."""


__all__ = [
    "CausalityViolationError",
    "FeatureComputationError",
    "FeatureConfigurationError",
    "FeatureError",
    "FeatureValidationError",
]
