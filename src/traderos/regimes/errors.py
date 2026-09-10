"""Explicit failures for the deterministic regime boundary."""

from traderos.core.errors import TraderosError


class RegimeError(TraderosError):
    """Base class for expected regime-domain failures."""


class RegimeConfigurationError(RegimeError):
    """Raised when detector metadata or parameters are unsafe."""


class RegimeCausalityError(RegimeError):
    """Raised when a regime context contains unavailable information."""


class RegimeRegistryError(RegimeError):
    """Raised when a detector registry identity or scope is invalid."""


class RegimeAnalysisError(RegimeError):
    """Raised when descriptive regime-history analysis is invalid."""


__all__ = [
    "RegimeAnalysisError",
    "RegimeCausalityError",
    "RegimeConfigurationError",
    "RegimeError",
    "RegimeRegistryError",
]
