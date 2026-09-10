"""Provider-neutral interface owned by the data domain."""

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from traderos.data.bars import BarCandidate
from traderos.data.instruments import Instrument
from traderos.data.time import require_utc
from traderos.data.timeframes import Timeframe, require_supported_timeframe


class HistoricalBarsRequest(BaseModel):
    """Bounded historical-data request passed to a provider adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument: Instrument
    timeframe: Timeframe
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def validate_request(self) -> "HistoricalBarsRequest":
        require_supported_timeframe(self.instrument.asset_class, self.timeframe)
        require_utc(self.start)
        require_utc(self.end)
        if self.start >= self.end:
            raise ValueError("request start must be before request end")
        return self


class ProviderHealth(BaseModel):
    """Safe provider health result with no credential content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1)
    healthy: bool
    message: str = Field(min_length=1)


class MarketDataProvider(Protocol):
    """Provider adapter contract.

    Adapters parse vendor payloads internally and expose only the provider-neutral
    ``BarCandidate`` shape to ingestion. Valid canonical ``MarketBar`` objects
    are produced only after normalization and validation.
    """

    provider_id: str

    def get_instruments(self) -> tuple[Instrument, ...]:
        """Return canonical instruments available from the provider."""

    def get_historical_bars(self, request: HistoricalBarsRequest) -> tuple[BarCandidate, ...]:
        """Return provider-neutral candidates for a bounded request."""

    def get_latest_bar(self, instrument: Instrument, timeframe: Timeframe) -> BarCandidate | None:
        """Return the latest candidate for an instrument/timeframe pair."""

    def health_check(self) -> ProviderHealth:
        """Return provider health without exposing secrets."""


__all__ = ["HistoricalBarsRequest", "MarketDataProvider", "ProviderHealth"]
