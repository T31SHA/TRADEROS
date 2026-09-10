"""Canonical model and validation invariants."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from traderos.data.bars import BarCandidate, MarketBar
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.timeframes import Timeframe, is_supported_timeframe


def equity() -> Instrument:
    return Instrument(
        canonical_symbol="AAPL",
        asset_class=AssetClass.EQUITY,
        provider_symbols={"local": "AAPL"},
        exchange="NASDAQ",
        trading_currency="USD",
        timezone="America/New_York",
    )


def test_provider_symbol_mapping_stays_on_instrument() -> None:
    instrument = equity()

    assert instrument.provider_symbol("local") == "AAPL"
    with pytest.raises(KeyError):
        instrument.provider_symbol("unconfigured")


def test_timeframe_compatibility_is_explicit() -> None:
    assert is_supported_timeframe(AssetClass.EQUITY, Timeframe.H1)
    assert not is_supported_timeframe(AssetClass.EQUITY, Timeframe.M15)


def test_valid_canonical_bar_exposes_symbol_and_asset_class() -> None:
    timestamp = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    bar = MarketBar(
        instrument=equity(),
        timeframe=Timeframe.H1,
        timestamp=timestamp,
        open=Decimal("250"),
        high=Decimal("255"),
        low=Decimal("249"),
        close=Decimal("254"),
        volume=Decimal("1000"),
        source="local",
        currency="USD",
        ingestion_timestamp=timestamp + timedelta(minutes=1),
    )

    assert bar.symbol == "AAPL"
    assert bar.asset_class is AssetClass.EQUITY
    assert bar.logical_key == ("AAPL", Timeframe.H1, timestamp, "local", "raw")


@pytest.mark.parametrize(
    ("high", "low"),
    [("249", "249"), ("255", "256")],
)
def test_invalid_ohlc_is_rejected(high: str, low: str) -> None:
    with pytest.raises((ValidationError, ValueError)):
        MarketBar(
            instrument=equity(),
            timeframe=Timeframe.H1,
            timestamp=datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
            open=Decimal("250"),
            high=Decimal(high),
            low=Decimal(low),
            close=Decimal("254"),
            source="local",
            ingestion_timestamp=datetime(2026, 1, 5, 15, 0, tzinfo=UTC),
        )


def test_untrusted_candidate_can_be_reported_by_validation_layer() -> None:
    candidate = BarCandidate.model_construct(
        instrument=equity(),
        timeframe=Timeframe.H1,
        timestamp=datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
        open=Decimal("250"),
        high=Decimal("249"),
        low=Decimal("248"),
        close=Decimal("249"),
    )

    assert candidate.high < candidate.open
