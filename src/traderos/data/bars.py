"""Untrusted bar candidates and validated canonical market bars."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from traderos.data.instruments import AssetClass, Instrument
from traderos.data.lineage import AdjustmentPolicy
from traderos.data.timeframes import Timeframe, require_supported_timeframe


def _finite_positive(value: Decimal, field_name: str) -> Decimal:
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{field_name} must be finite and positive")
    return value


class BarCandidate(BaseModel):
    """Provider-neutral but untrusted bar payload before normalization/validation."""

    model_config = ConfigDict(extra="forbid")

    instrument: Instrument
    timeframe: Timeframe
    adjustment_policy: AdjustmentPolicy = AdjustmentPolicy.RAW
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None
    source_timezone: str | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    spread: Decimal | None = None
    adjusted_close: Decimal | None = None
    trade_count: int | None = None
    vwap: Decimal | None = None


class MarketBar(BaseModel):
    """Canonical validated bar stored and consumed by downstream domains."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument: Instrument
    timeframe: Timeframe
    adjustment_policy: AdjustmentPolicy = AdjustmentPolicy.RAW
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = Field(default=None, ge=0)
    source: str = Field(min_length=1)
    currency: str | None = None
    ingestion_timestamp: datetime
    bid: Decimal | None = Field(default=None, gt=0)
    ask: Decimal | None = Field(default=None, gt=0)
    spread: Decimal | None = Field(default=None, ge=0)
    adjusted_close: Decimal | None = Field(default=None, gt=0)
    trade_count: int | None = Field(default=None, ge=0)
    vwap: Decimal | None = Field(default=None, gt=0)

    @field_validator("timestamp", "ingestion_timestamp")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None:
            raise ValueError("timestamps must be timezone-aware")
        if offset.total_seconds() != 0:
            raise ValueError("canonical timestamps must be UTC")
        return value

    @field_validator("source")
    @classmethod
    def strip_source(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("source must not be blank")
        return cleaned

    @model_validator(mode="after")
    def validate_invariants(self) -> "MarketBar":
        require_supported_timeframe(self.instrument.asset_class, self.timeframe)
        for name in ("open", "high", "low", "close"):
            _finite_positive(getattr(self, name), name)
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be at least open, close, and low")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must be at most open, close, and high")
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("bid must not exceed ask")
        return self

    @property
    def symbol(self) -> str:
        """Canonical symbol convenience property."""

        return self.instrument.canonical_symbol

    @property
    def asset_class(self) -> AssetClass:
        """Canonical asset class convenience property."""

        return self.instrument.asset_class

    @property
    def logical_key(self) -> tuple[str, Timeframe, datetime, str, AdjustmentPolicy]:
        """Deterministic storage identity for a source-aware bar."""

        return (
            self.symbol,
            self.timeframe,
            self.timestamp,
            self.source,
            self.adjustment_policy,
        )


__all__ = ["BarCandidate", "MarketBar"]
