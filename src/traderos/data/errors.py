"""Explicit market-data error categories."""

from traderos.core.errors import TraderosError


class DataError(TraderosError):
    """Base class for expected data-subsystem errors."""


class DataValidationError(DataError):
    """Raised when a candidate cannot become canonical market data."""


class TimestampNormalizationError(DataValidationError):
    """Raised when a timestamp cannot be safely converted to UTC."""


class ProviderError(DataError):
    """Base class for provider failures."""


class TransientProviderError(ProviderError):
    """A provider failure that may succeed on a bounded retry."""


class ProviderAuthenticationError(ProviderError):
    """Provider credentials are missing or invalid."""


class ProviderRateLimitError(TransientProviderError):
    """The provider requested a retry after rate limiting."""


class ProviderNetworkError(TransientProviderError):
    """A transient provider network failure."""


class ProviderTimeoutError(TransientProviderError):
    """A provider request timed out."""


class MalformedProviderResponseError(ProviderError):
    """Provider response cannot be parsed into a bar candidate."""


class StorageError(DataError):
    """Storage could not persist or retrieve market data."""


class IngestionError(DataError):
    """An ingestion run failed after its run record was created."""
