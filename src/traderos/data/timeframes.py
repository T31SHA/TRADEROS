"""Canonical timeframe definitions and asset compatibility."""

from datetime import timedelta
from enum import StrEnum

from traderos.data.instruments import AssetClass


class Timeframe(StrEnum):
    """Supported Phase 1 bar intervals."""

    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def duration(self) -> timedelta:
        return {
            Timeframe.M15: timedelta(minutes=15),
            Timeframe.H1: timedelta(hours=1),
            Timeframe.H4: timedelta(hours=4),
            Timeframe.D1: timedelta(days=1),
        }[self]


SUPPORTED_TIMEFRAMES: dict[AssetClass, frozenset[Timeframe]] = {
    AssetClass.FOREX: frozenset({Timeframe.M15, Timeframe.H1, Timeframe.H4, Timeframe.D1}),
    AssetClass.EQUITY: frozenset({Timeframe.H1, Timeframe.D1}),
    AssetClass.ETF: frozenset({Timeframe.H1, Timeframe.D1}),
}


def is_supported_timeframe(asset_class: AssetClass, timeframe: Timeframe) -> bool:
    """Return whether the timeframe is allowed for the asset class."""

    return timeframe in SUPPORTED_TIMEFRAMES[asset_class]


def require_supported_timeframe(asset_class: AssetClass, timeframe: Timeframe) -> None:
    """Raise a clear error for an unsupported asset/timeframe pair."""

    if not is_supported_timeframe(asset_class, timeframe):
        raise ValueError(f"{timeframe.value} is not supported for {asset_class.value}")


__all__ = [
    "SUPPORTED_TIMEFRAMES",
    "Timeframe",
    "is_supported_timeframe",
    "require_supported_timeframe",
]
