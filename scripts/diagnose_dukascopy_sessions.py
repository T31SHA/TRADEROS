"""Diagnose v2 missing intervals and zero-volume bars under two calendars.

This is an offline evidence tool. It reads the already-present raw CSV and
does not preserve, rewrite, download, or normalize the source artifact.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from traderos.data.calendars import DukascopyForexCalendar, ForexCalendar, MarketCalendar

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data/raw/dukascopy/EURUSD/v2/eurusd-m15-bid-2024-01-01-2025-01-01.csv"
TIMEFRAME = timedelta(minutes=15)
DISPLAY_TIMEZONE = ZoneInfo("America/New_York")


def _timestamp(value: str) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


def _dst_state(timestamp: datetime) -> str:
    return "summer_dst" if timestamp.astimezone(DISPLAY_TIMEZONE).dst() else "winter_standard"


def _distribution(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _missing_runs(actual: set[datetime], start: datetime, end: datetime) -> list[list[datetime]]:
    runs: list[list[datetime]] = []
    current = start
    while current < end:
        if current not in actual:
            if not runs or runs[-1][-1] + TIMEFRAME != current:
                runs.append([])
            runs[-1].append(current)
        current += TIMEFRAME
    return runs


def _consecutive_runs(values: set[datetime]) -> list[list[datetime]]:
    runs: list[list[datetime]] = []
    for timestamp in sorted(values):
        if not runs or runs[-1][-1] + TIMEFRAME != timestamp:
            runs.append([])
        runs[-1].append(timestamp)
    return runs


def _calendar_report(
    *,
    calendar: MarketCalendar,
    actual: set[datetime],
    zero_rows: dict[datetime, tuple[str, str, str, str]],
    start: datetime,
    end: datetime,
) -> dict[str, object]:
    missing_runs = _missing_runs(actual, start, end)
    sorted_actual = sorted(actual)
    run_reports: list[dict[str, object]] = []
    unexpected_missing: list[datetime] = []
    for run in missing_runs:
        previous = max(timestamp for timestamp in sorted_actual if timestamp < run[0])
        following = min(timestamp for timestamp in sorted_actual if timestamp > run[-1])
        classifications = {
            "expected_market_closure": 0,
            "data_gap": 0,
            "unknown": 0,
        }
        for timestamp in run:
            try:
                open_at = calendar.is_open_at(timestamp)
            except (TypeError, ValueError, OverflowError):
                classification = "unknown"
            else:
                classification = "data_gap" if open_at else "expected_market_closure"
            classifications[classification] += 1
            if classification == "data_gap":
                unexpected_missing.append(timestamp)
        if classifications["data_gap"]:
            run_reports.append(
                {
                    "previous_timestamp": previous.isoformat(),
                    "next_timestamp": following.isoformat(),
                    "missing_intervals": len(run),
                    "unexpected_intervals": classifications["data_gap"],
                    "expected_closure_intervals": classifications["expected_market_closure"],
                    "unknown_intervals": classifications["unknown"],
                    "first_missing": run[0].isoformat(),
                    "last_missing": run[-1].isoformat(),
                }
            )

    zero_active = [timestamp for timestamp in zero_rows if calendar.is_open_at(timestamp)]
    zero_closed = [timestamp for timestamp in zero_rows if not calendar.is_open_at(timestamp)]
    zero_active_set = set(zero_active)
    zero_runs = _consecutive_runs(zero_active_set)
    zero_movement = [
        "flat" if len(set(zero_rows[timestamp])) == 1 else "moving" for timestamp in zero_active
    ]

    return {
        "calendar_id": calendar.calendar_id,
        "unexpected_gap_count": len(unexpected_missing),
        "unexpected_gap_run_count": len(run_reports),
        "unexpected_gaps": run_reports,
        "gap_distribution": {
            "hour_utc": _distribution([str(timestamp.hour) for timestamp in unexpected_missing]),
            "day_of_week": _distribution(
                [timestamp.strftime("%A") for timestamp in unexpected_missing]
            ),
            "month": _distribution(
                [timestamp.strftime("%Y-%m") for timestamp in unexpected_missing]
            ),
            "dst_state": _distribution(
                [_dst_state(timestamp) for timestamp in unexpected_missing]
            ),
            "gap_size_intervals": _distribution(
                [str(item["missing_intervals"]) for item in run_reports]
            ),
        },
        "zero_volume": {
            "total": len(zero_rows),
            "active_session": len(zero_active),
            "closed_session": len(zero_closed),
            "hour_utc": _distribution([str(timestamp.hour) for timestamp in zero_active]),
            "day_of_week": _distribution(
                [timestamp.strftime("%A") for timestamp in zero_active]
            ),
            "month": _distribution([timestamp.strftime("%Y-%m") for timestamp in zero_active]),
            "dst_state": _distribution([_dst_state(timestamp) for timestamp in zero_active]),
            "consecutive_run_lengths": _distribution([str(len(run)) for run in zero_runs]),
            "ohlc_movement": _distribution(zero_movement),
        },
    }


def main() -> int:
    if not SOURCE.is_file():
        raise FileNotFoundError(f"local raw artifact not found: {SOURCE}")
    actual: set[datetime] = set()
    zero_rows: dict[datetime, tuple[str, str, str, str]] = {}
    with SOURCE.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            timestamp = _timestamp(row["timestamp"])
            actual.add(timestamp)
            if row["volume"] == "0":
                zero_rows[timestamp] = (row["open"], row["high"], row["low"], row["close"])
    start = min(actual)
    end = max(actual) + TIMEFRAME
    payload = {
        "source": str(SOURCE),
        "raw_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        "raw_rows": len(actual),
        "coverage_start": start.isoformat(),
        "coverage_end": end.isoformat(),
        "calendars": [
            _calendar_report(
                calendar=calendar,
                actual=actual,
                zero_rows=zero_rows,
                start=start,
                end=end,
            )
            for calendar in (ForexCalendar(), DukascopyForexCalendar())
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
