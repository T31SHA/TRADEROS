"""Chronological train/validation/test fold construction with explicit gaps."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from traderos.research.models import ResearchError, TemporalRange


@dataclass(frozen=True)
class ValidationFold:
    fold_index: int
    train: TemporalRange
    validation: TemporalRange
    test: TemporalRange
    purge: timedelta
    embargo: timedelta

    def __post_init__(self) -> None:
        if self.fold_index < 0 or self.purge < timedelta(0) or self.embargo < timedelta(0):
            raise ResearchError("fold index and purge/embargo durations must be non-negative")
        if self.train.end + self.purge > self.validation.start:
            raise ResearchError("validation begins before the configured purge completes")
        if self.validation.end + self.embargo > self.test.start:
            raise ResearchError("test begins before the configured embargo completes")


@dataclass(frozen=True)
class ChronologicalValidationProtocol:
    """Predeclared time-series protocol; it has no shuffle or optimization mode."""

    protocol_id: str
    version: str
    train_duration: timedelta
    validation_duration: timedelta
    test_duration: timedelta
    step: timedelta
    purge: timedelta = timedelta(0)
    embargo: timedelta = timedelta(0)
    fold_count: int = 1
    dependency_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.protocol_id.strip() or not self.version.strip():
            raise ResearchError("validation protocol identity must not be blank")
        durations = (
            self.train_duration,
            self.validation_duration,
            self.test_duration,
            self.step,
        )
        if any(value <= timedelta(0) for value in durations):
            raise ResearchError("train, validation, test, and step durations must be positive")
        if self.purge < timedelta(0) or self.embargo < timedelta(0) or self.fold_count < 1:
            raise ResearchError("purge/embargo must be non-negative and fold count positive")
        if (self.purge or self.embargo) and not (
            self.dependency_reason and self.dependency_reason.strip()
        ):
            raise ResearchError("purge or embargo requires a documented dependency reason")

    def folds(self, *, start: TemporalRange, boundary: TemporalRange) -> tuple[ValidationFold, ...]:
        """Produce strictly chronological folds contained in development data."""
        if start.start != boundary.start:
            raise ResearchError("fold generation start must equal the development boundary start")
        output: list[ValidationFold] = []
        train_start = start.start
        for index in range(self.fold_count):
            train_end = train_start + self.train_duration
            validation_start = train_end + self.purge
            validation_end = validation_start + self.validation_duration
            test_start = validation_end + self.embargo
            test_end = test_start + self.test_duration
            if test_end > boundary.end:
                raise ResearchError("validation fold exceeds the declared development boundary")
            output.append(
                ValidationFold(
                    index,
                    TemporalRange(train_start, train_end),
                    TemporalRange(validation_start, validation_end),
                    TemporalRange(test_start, test_end),
                    self.purge,
                    self.embargo,
                )
            )
            train_start += self.step
        return tuple(output)


__all__ = ["ChronologicalValidationProtocol", "ValidationFold"]
