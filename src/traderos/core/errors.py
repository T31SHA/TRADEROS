"""Application-specific error types."""


class TraderosError(Exception):
    """Base class for expected TRADEROS errors."""


class ConfigurationError(TraderosError):
    """Raised when configuration violates a safety or validity invariant."""
