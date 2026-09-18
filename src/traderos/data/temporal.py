"""Provider-neutral timestamp declarations used by historical importers."""

from enum import StrEnum


class TimestampSemantics(StrEnum):
    """The source meaning of a timestamp; importers must declare it."""

    BAR_START = "bar_start"
    BAR_END = "bar_end"


class TimestampFormat(StrEnum):
    """The declared representation of timestamps in a source artifact."""

    ISO_8601 = "iso_8601"
    EPOCH_MILLISECONDS = "epoch_milliseconds"


__all__ = ["TimestampFormat", "TimestampSemantics"]
