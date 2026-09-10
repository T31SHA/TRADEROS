"""Explicit timestamp normalization policy."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from traderos.data.errors import TimestampNormalizationError


def utc_now() -> datetime:
    """Return the current timezone-aware UTC instant."""

    return datetime.now(UTC)


def _localize_unambiguous(value: datetime, source_timezone: str) -> datetime:
    try:
        timezone = ZoneInfo(source_timezone)
    except ZoneInfoNotFoundError as exc:
        raise TimestampNormalizationError(f"Unknown source timezone: {source_timezone}") from exc

    candidates: set[datetime] = set()
    for fold in (0, 1):
        localized = value.replace(tzinfo=timezone, fold=fold)
        candidate = localized.astimezone(UTC)
        if candidate.astimezone(timezone).replace(tzinfo=None) == value:
            candidates.add(candidate)
    if not candidates:
        raise TimestampNormalizationError(
            f"Nonexistent local timestamp {value.isoformat()} in {source_timezone}"
        )
    if len(candidates) > 1:
        raise TimestampNormalizationError(
            f"Ambiguous local timestamp {value.isoformat()} in {source_timezone}"
        )
    return candidates.pop()


def normalize_timestamp(value: datetime, source_timezone: str | None = None) -> datetime:
    """Normalize an aware timestamp to UTC or reject an unsafe naive timestamp."""

    if value.tzinfo is None or value.utcoffset() is None:
        if source_timezone is None:
            raise TimestampNormalizationError(
                "Naive timestamps require an explicit source_timezone; they are never assumed UTC"
            )
        return _localize_unambiguous(value, source_timezone)
    return value.astimezone(UTC)


def require_utc(value: datetime) -> datetime:
    """Validate an already normalized UTC timestamp."""

    normalized = normalize_timestamp(value)
    if normalized != value:
        raise TimestampNormalizationError("timestamp is not represented in UTC")
    return normalized


__all__ = ["normalize_timestamp", "require_utc", "utc_now"]
