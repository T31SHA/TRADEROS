"""Timezone and DST safety tests."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from traderos.data.errors import TimestampNormalizationError
from traderos.data.time import normalize_timestamp


def test_aware_timestamp_is_converted_to_utc() -> None:
    timestamp = datetime(2026, 1, 5, 17, 30, tzinfo=ZoneInfo("Africa/Nairobi"))

    assert normalize_timestamp(timestamp) == datetime(2026, 1, 5, 14, 30, tzinfo=UTC)


def test_naive_timestamp_is_rejected_without_explicit_source_timezone() -> None:
    with pytest.raises(TimestampNormalizationError, match="never assumed UTC"):
        normalize_timestamp(datetime(2026, 1, 5, 14, 30))


def test_naive_timestamp_can_be_normalized_with_explicit_timezone() -> None:
    timestamp = datetime(2026, 1, 5, 9, 30)

    assert normalize_timestamp(timestamp, "America/New_York") == datetime(
        2026, 1, 5, 14, 30, tzinfo=UTC
    )


@pytest.mark.parametrize(
    "timestamp",
    [datetime(2026, 11, 1, 1, 30), datetime(2026, 3, 8, 2, 30)],
)
def test_ambiguous_and_nonexistent_dst_times_are_rejected(timestamp: datetime) -> None:
    with pytest.raises(TimestampNormalizationError):
        normalize_timestamp(timestamp, "America/New_York")
