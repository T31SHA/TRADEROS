"""Explicit failures for the deterministic risk-firewall boundary."""

from traderos.core.errors import TraderosError


class RiskError(TraderosError):
    """Base class for expected risk-domain failures."""


class RiskConfigurationError(RiskError):
    """Raised when a risk-firewall policy cannot be safely constructed."""


class RiskCausalityError(RiskError):
    """Raised when a caller requires strict context construction validation."""


__all__ = ["RiskCausalityError", "RiskConfigurationError", "RiskError"]
