"""Calendar-aware quality controls."""

from datetime import UTC, date, datetime
from decimal import Decimal

from traderos.data.bars import MarketBar
from traderos.data.calendars import (
    DukascopyForexCalendar,
    ForexCalendar,
    UsEquityCalendar,
    calendar_for,
)
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.quality import DataQualityEngine, QualityCode, QualitySeverity
from traderos.data.timeframes import Timeframe


def equity() -> Instrument:
    return Instrument(
        canonical_symbol="AAPL",
        asset_class=AssetClass.EQUITY,
        trading_currency="USD",
        timezone="America/New_York",
    )


def bar(timestamp: datetime, close: str = "100") -> MarketBar:
    return MarketBar(
        instrument=equity(),
        timeframe=Timeframe.H1,
        timestamp=timestamp,
        open=Decimal(close),
        high=Decimal(close) + 1,
        low=Decimal(close) - 1,
        close=Decimal(close),
        source="local",
        currency="USD",
        ingestion_timestamp=datetime(2026, 1, 5, 15, 0, tzinfo=UTC),
    )


def test_forex_weekend_is_closed_but_sunday_open_is_valid() -> None:
    calendar = ForexCalendar()

    assert not calendar.is_open_at(datetime(2026, 1, 10, 12, tzinfo=UTC))
    assert not calendar.is_open_at(datetime(2026, 1, 11, 21, 59, tzinfo=UTC))
    assert calendar.is_open_at(datetime(2026, 1, 11, 22, tzinfo=UTC))
    assert calendar.is_open_at(datetime(2026, 1, 9, 21, 59, tzinfo=UTC))
    assert not calendar.is_open_at(datetime(2026, 1, 9, 22, tzinfo=UTC))
    assert calendar.expected_bar_timestamps(
        datetime(2026, 1, 9, 21, 45, tzinfo=UTC),
        datetime(2026, 1, 9, 22, 15, tzinfo=UTC),
        Timeframe.M15,
    ) == (datetime(2026, 1, 9, 21, 45, tzinfo=UTC),)


def test_dukascopy_winter_session_boundaries_are_utc_22() -> None:
    calendar = DukascopyForexCalendar()

    assert calendar.is_open_at(datetime(2024, 1, 5, 21, 45, tzinfo=UTC))
    assert not calendar.is_open_at(datetime(2024, 1, 5, 22, tzinfo=UTC))
    assert not calendar.is_open_at(datetime(2024, 1, 7, 21, 45, tzinfo=UTC))
    assert calendar.is_open_at(datetime(2024, 1, 7, 22, tzinfo=UTC))


def test_dukascopy_summer_session_boundaries_are_utc_21() -> None:
    calendar = DukascopyForexCalendar()

    assert calendar.is_open_at(datetime(2024, 3, 15, 20, 45, tzinfo=UTC))
    assert not calendar.is_open_at(datetime(2024, 3, 15, 21, tzinfo=UTC))
    assert not calendar.is_open_at(datetime(2024, 3, 17, 20, 45, tzinfo=UTC))
    assert calendar.is_open_at(datetime(2024, 3, 17, 21, tzinfo=UTC))


def test_dukascopy_dst_rules_change_without_date_lists() -> None:
    calendar = DukascopyForexCalendar()

    # The IANA America/New_York rules move the boundary for the 2024 DST
    # transition dates without any calendar-specific hardcoding.
    assert calendar.is_open_at(datetime(2024, 3, 10, 21, tzinfo=UTC))
    assert not calendar.is_open_at(datetime(2024, 11, 3, 21, 45, tzinfo=UTC))
    assert calendar.is_open_at(datetime(2024, 11, 3, 22, tzinfo=UTC))


def test_dukascopy_weekday_is_continuously_open() -> None:
    calendar = DukascopyForexCalendar()

    assert all(
        calendar.is_open_at(datetime(2024, 6, 6, hour, 0, tzinfo=UTC))
        for hour in range(24)
    )


def test_dukascopy_15m_gap_classification_uses_bar_start_semantics() -> None:
    calendar = DukascopyForexCalendar()

    winter = calendar.expected_bar_timestamps(
        datetime(2024, 1, 5, 21, 45, tzinfo=UTC),
        datetime(2024, 1, 7, 22, 15, tzinfo=UTC),
        Timeframe.M15,
    )
    summer = calendar.expected_bar_timestamps(
        datetime(2024, 3, 15, 20, 45, tzinfo=UTC),
        datetime(2024, 3, 17, 21, 15, tzinfo=UTC),
        Timeframe.M15,
    )

    assert winter == (
        datetime(2024, 1, 5, 21, 45, tzinfo=UTC),
        datetime(2024, 1, 7, 22, 0, tzinfo=UTC),
    )
    assert summer == (
        datetime(2024, 3, 15, 20, 45, tzinfo=UTC),
        datetime(2024, 3, 17, 21, 0, tzinfo=UTC),
    )


def test_dukascopy_calendar_identity_is_explicit_and_distinct() -> None:
    assert DukascopyForexCalendar.calendar_id == "dukascopy-forex-utc-session-v1"
    assert DukascopyForexCalendar.calendar_id != ForexCalendar.calendar_id


def test_calendar_factory_and_daily_equity_session_are_explicit() -> None:
    assert isinstance(calendar_for(AssetClass.FOREX), ForexCalendar)
    equity_calendar = calendar_for(AssetClass.EQUITY, holidays={date(2024, 6, 6)})

    assert equity_calendar.calendar_id == "us-equity-regular-v1"
    assert equity_calendar.is_open_at(datetime(2024, 6, 5, 14, 30, tzinfo=UTC))
    assert not equity_calendar.is_open_at(datetime(2024, 6, 6, 14, 30, tzinfo=UTC))
    assert equity_calendar.expected_bar_timestamps(
        datetime(2024, 6, 5, 13, 30, tzinfo=UTC),
        datetime(2024, 6, 7, 13, 30, tzinfo=UTC),
        Timeframe.D1,
    ) == (datetime(2024, 6, 5, 13, 30, tzinfo=UTC),)


def test_equity_gap_detector_ignores_market_closure() -> None:
    calendar = UsEquityCalendar(holidays={date(2026, 1, 6)})
    start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    end = datetime(2026, 1, 7, 14, 30, tzinfo=UTC)
    expected = calendar.expected_bar_timestamps(start, end, Timeframe.H1)
    report = DataQualityEngine().inspect(
        [bar(timestamp) for timestamp in expected],
        calendar=calendar,
        start=start,
        end=end,
        occurred_at=end,
    )

    assert not any(event.code is QualityCode.MISSING_BAR for event in report.events)


def test_equity_gap_detector_flags_missing_in_session_bar() -> None:
    calendar = UsEquityCalendar()
    start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    end = datetime(2026, 1, 5, 17, 30, tzinfo=UTC)
    expected = calendar.expected_bar_timestamps(start, end, Timeframe.H1)
    report = DataQualityEngine().inspect(
        [bar(expected[0]), bar(expected[2])],
        calendar=calendar,
        start=start,
        end=end,
        occurred_at=end,
    )

    missing = [event for event in report.events if event.code is QualityCode.MISSING_BAR]
    assert len(missing) == 1
    assert missing[0].severity is QualitySeverity.ERROR


def test_quality_engine_flags_duplicates_and_suspicious_jumps() -> None:
    calendar = UsEquityCalendar()
    first = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    second = datetime(2026, 1, 5, 15, 30, tzinfo=UTC)
    report = DataQualityEngine().inspect(
        [bar(first), bar(first), bar(second, close="150")],
        calendar=calendar,
        start=first,
        end=datetime(2026, 1, 5, 16, 30, tzinfo=UTC),
        occurred_at=second,
    )

    assert any(event.code is QualityCode.DUPLICATE_BAR for event in report.events)
    assert any(event.code is QualityCode.SUSPICIOUS_JUMP for event in report.events)


def test_freshness_is_a_hard_error_event() -> None:
    event = DataQualityEngine().check_freshness(
        None,
        expected_latest=datetime(2026, 1, 5, 15, 30, tzinfo=UTC),
        occurred_at=datetime(2026, 1, 5, 15, 31, tzinfo=UTC),
    )

    assert event is not None
    assert event.code is QualityCode.STALE_DATA
    assert event.severity is QualitySeverity.ERROR
