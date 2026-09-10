"""Canonical tradable-instrument representation."""

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AssetClass(StrEnum):
    """Asset classes supported by the initial data foundation."""

    FOREX = "forex"
    EQUITY = "equity"
    ETF = "etf"


class Instrument(BaseModel):
    """Provider-independent instrument metadata.

    ``provider_symbols`` is the only place a provider-specific symbol mapping
    belongs. Domain consumers use ``canonical_symbol`` and never provider codes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    canonical_symbol: str = Field(min_length=1)
    asset_class: AssetClass
    provider_symbols: dict[str, str] = Field(default_factory=dict)
    exchange: str | None = None
    base_currency: str | None = None
    quote_currency: str | None = None
    trading_currency: str | None = None
    timezone: str = Field(default="UTC", min_length=1)
    tick_size: Decimal | None = Field(default=None, gt=0)
    lot_size: Decimal | None = Field(default=None, gt=0)
    is_active: bool = True

    @field_validator(
        "canonical_symbol",
        "exchange",
        "base_currency",
        "quote_currency",
        "trading_currency",
        mode="before",
    )
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("text fields must not be blank")
        return cleaned

    def provider_symbol(self, provider: str) -> str:
        """Return a configured provider symbol or fail explicitly."""

        try:
            return self.provider_symbols[provider]
        except KeyError as exc:
            raise KeyError(
                f"No provider symbol configured for {self.canonical_symbol!r} and {provider!r}"
            ) from exc


__all__ = ["AssetClass", "Instrument"]
