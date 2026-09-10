"""Explicit failures for deterministic signal fusion."""

from traderos.core.errors import TraderosError


class SignalFusionError(TraderosError):
    """Base class for expected signal-fusion failures."""


class FusionConfigurationError(SignalFusionError):
    """Raised for invalid policy parameters or compatibility rules."""


class FusionCausalityError(SignalFusionError):
    """Raised when a context violates strict decision-time alignment."""


class FutureSignalError(FusionCausalityError):
    """Raised when a future strategy signal is supplied to fusion."""


class FutureRegimeError(FusionCausalityError):
    """Raised when a future regime state is supplied to fusion."""


class IncompatibleSignalError(SignalFusionError):
    """Raised when signal scope differs from the single fusion scope."""


class DuplicateStrategySignalError(SignalFusionError):
    """Raised for conflicting signal identities from one strategy instance."""


class FusionPolicyRegistryError(SignalFusionError):
    """Raised when a versioned fusion policy cannot be registered or resolved."""


__all__ = [
    "DuplicateStrategySignalError",
    "FusionCausalityError",
    "FusionConfigurationError",
    "FusionPolicyRegistryError",
    "FutureRegimeError",
    "FutureSignalError",
    "IncompatibleSignalError",
    "SignalFusionError",
]
