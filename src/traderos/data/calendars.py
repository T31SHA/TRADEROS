"""Minimal, explicit market-calendar abstractions for Phase 1.

Calendars use bar-start semantics: a timestamp identifies the opening instant
of a bar. Holiday data is injected into a calendar rather than scattered
through the data or strategy code.
"""

from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from traderos.data.instruments import AssetClass
from traderos.data.time import normalize_timestamp
from traderos.data.timeframes import Timeframe


class MarketCalendar(Protocol):
    """Calendar contract used by quality checks and gap detection."""

    calendar_id: str

    def is_open_at(self, timestamp: datetime) -> bool:
        """Return whether a UTC instant is within a trading session."""

    def expected_bar_timestamps(
        self,
        start: datetime,
        end: datetime,
        timeframe: Timeframe,
    ) -> tuple[datetime, ...]:
        """Return expected bar-start timestamps in ``[start, end)``."""


def _date_range(start: date, end: date) -> Iterator[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _utc_range(start: datetime, end: datetime, step: timedelta) -> Iterator[datetime]:
    current = start
    while current < end:
        yield current
        current += step


class ForexCalendar:
    """A conservative 24-hour weekday Forex calendar.

    The V1 policy models the common weekend closure from Friday 22:00 UTC to
    Sunday 22:00 UTC. Broker-specific holidays, rollover breaks, and DST-aware
    venue rules remain an explicitly documented future limitation.
    """

    calendar_id = "forex-weekday-utc-v1"

    def is_open_at(self, timestamp: datetime) -> bool:
        utc_timestamp = normalize_timestamp(timestamp)
        weekday = utc_timestamp.weekday()
        clock = utc_timestamp.time()
        if weekday == 5:  # Saturday
            return False
        if weekday == 6:  # Sunday, opens at 22:00 UTC
            return clock >= time(22, 0)
        if weekday == 4:  # Friday, closes at 22:00 UTC
            return clock < time(22, 0)
        return True

    def expected_bar_timestamps(
        self,
        start: datetime,
        end: datetime,
        timeframe: Timeframe,
    ) -> tuple[datetime, ...]:
        start_utc = normalize_timestamp(start)
        end_utc = normalize_timestamp(end)
        return tuple(
            timestamp
            for timestamp in _utc_range(start_utc, end_utc, timeframe.duration)
            if self.is_open_at(timestamp)
        )


class DukascopyForexCalendar(ForexCalendar):
    """Dukascopy EUR/USD weekly session with an explicit DST timezone.

    Dukascopy's GMT session boundary is 21:00 during summer time and 22:00
    during winter time.  The documented 17:00 Eastern weekly boundary maps to
    those UTC instants through the IANA ``America/New_York`` rules.  The
    original :class:`ForexCalendar` remains unchanged for backward-compatible
    Phase 1 behavior; this calendar is an explicit empirical-source version.
    """

    calendar_id = "dukascopy-forex-utc-session-v1"
    _timezone = ZoneInfo("America/New_York")
    _session_boundary = time(17, 0)

    def is_open_at(self, timestamp: datetime) -> bool:
        local_timestamp = normalize_timestamp(timestamp).astimezone(self._timezone)
        local_time = local_timestamp.time()
        if local_timestamp.weekday() == 5:  # Saturday
            return False
        if local_timestamp.weekday() == 6:  # Sunday, opens at 17:00 local
            return local_time >= self._session_boundary
        if local_timestamp.weekday() == 4:  # Friday, closes at 17:00 local
            return local_time < self._session_boundary
        return True


class UsEquityCalendar:
    """US regular-session calendar with injected holiday dates.

    The calendar does not silently invent an exchange holiday source. Callers
    supply holidays in the venue's local date space. Early closes are supported
    through ``early_closes`` as local closing times.
    """

    calendar_id = "us-equity-regular-v1"
    _timezone = ZoneInfo("America/New_York")
    _open = time(9, 30)
    _close = time(16, 0)

    def __init__(
        self,
        holidays: Iterable[date] = (),
        early_closes: dict[date, time] | None = None,
    ) -> None:
        self._holidays = frozenset(holidays)
        self._early_closes = dict(early_closes or {})

    def _session(self, local_date: date) -> tuple[datetime, datetime] | None:
        if local_date.weekday() >= 5 or local_date in self._holidays:
            return None
        close = self._early_closes.get(local_date, self._close)
        local_open = datetime.combine(local_date, self._open, self._timezone)
        local_close = datetime.combine(local_date, close, self._timezone)
        return local_open.astimezone(UTC), local_close.astimezone(UTC)

    def is_open_at(self, timestamp: datetime) -> bool:
        utc_timestamp = normalize_timestamp(timestamp)
        local_date = utc_timestamp.astimezone(self._timezone).date()
        session = self._session(local_date)
        return session is not None and session[0] <= utc_timestamp < session[1]

    def expected_bar_timestamps(
        self,
        start: datetime,
        end: datetime,
        timeframe: Timeframe,
    ) -> tuple[datetime, ...]:
        start_utc = normalize_timestamp(start)
        end_utc = normalize_timestamp(end)
        local_start = start_utc.astimezone(self._timezone).date() - timedelta(days=1)
        local_end = end_utc.astimezone(self._timezone).date() + timedelta(days=1)
        expected: list[datetime] = []
        for local_date in _date_range(local_start, local_end):
            session = self._session(local_date)
            if session is None:
                continue
            if timeframe is Timeframe.D1:
                candidates: Sequence[datetime] = (session[0],)
            else:
                candidates = tuple(_utc_range(session[0], session[1], timeframe.duration))
            expected.extend(
                timestamp for timestamp in candidates if start_utc <= timestamp < end_utc
            )
        return tuple(sorted(expected))


def calendar_for(
    asset_class: AssetClass,
    *,
    holidays: Iterable[date] = (),
    early_closes: dict[date, time] | None = None,
) -> MarketCalendar:
    """Return the default calendar for an asset class."""

    if asset_class is AssetClass.FOREX:
        return ForexCalendar()
    return UsEquityCalendar(holidays=holidays, early_closes=early_closes)


__all__ = [
    "DukascopyForexCalendar",
    "ForexCalendar",
    "MarketCalendar",
    "UsEquityCalendar",
    "calendar_for",
]
