"""Optional data-quality comparison helpers for separate source datasets."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from statistics import median


@dataclass(frozen=True)
class CloseComparisonReport:
    """Close-price differences on overlapping timestamps only.

    ``mean_difference`` and percentile fields summarize absolute differences.
    ``signed_mean_difference`` is included to expose directional bias.  This
    report never merges datasets and contains no strategy or return metric.
    """

    source_a: str
    source_b: str
    overlap_count: int
    mean_difference: Decimal | None
    median_difference: Decimal | None
    p95_difference: Decimal | None
    p99_difference: Decimal | None
    max_difference: Decimal | None
    signed_mean_difference: Decimal | None

    def canonical(self) -> dict[str, object]:
        values = asdict(self)
        for key in (
            "mean_difference",
            "median_difference",
            "p95_difference",
            "p99_difference",
            "max_difference",
            "signed_mean_difference",
        ):
            value = values[key]
            values[key] = str(value) if value is not None else None
        return values

    @property
    def hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.canonical(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def _percentile(values: list[Decimal], fraction: Decimal) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[int((len(ordered) - 1) * float(fraction))]


def compare_close_series(
    first: Mapping[datetime, Decimal],
    second: Mapping[datetime, Decimal],
    *,
    source_a: str,
    source_b: str,
) -> CloseComparisonReport:
    """Compare two independent close series without selecting or combining them."""

    if not source_a.strip() or not source_b.strip():
        raise ValueError("comparison source names must not be blank")
    overlap = sorted(set(first).intersection(second))
    absolute = [abs(first[timestamp] - second[timestamp]) for timestamp in overlap]
    signed = [first[timestamp] - second[timestamp] for timestamp in overlap]
    with localcontext() as context:
        context.prec = 50
        signed_mean = sum(signed, Decimal("0")) / Decimal(len(signed)) if signed else None
        mean = sum(absolute, Decimal("0")) / Decimal(len(absolute)) if absolute else None
    return CloseComparisonReport(
        source_a=source_a,
        source_b=source_b,
        overlap_count=len(overlap),
        mean_difference=mean,
        median_difference=median(absolute) if absolute else None,
        p95_difference=_percentile(absolute, Decimal("0.95")),
        p99_difference=_percentile(absolute, Decimal("0.99")),
        max_difference=max(absolute) if absolute else None,
        signed_mean_difference=signed_mean,
    )


__all__ = ["CloseComparisonReport", "compare_close_series"]
