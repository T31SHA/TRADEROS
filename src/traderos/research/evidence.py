"""Canonical JSON evidence package export with no executable configuration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum

from traderos.research.models import DatasetManifest, ExperimentSpec, ResearchError


def _json_value(value: object) -> object:
    if isinstance(value, (datetime, date, timedelta, Decimal, Enum)):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ResearchError(f"evidence packages cannot serialize executable value {type(value)!r}")


@dataclass(frozen=True)
class EvidencePackage:
    """One immutable, machine-readable audit package for a research result."""

    experiment: ExperimentSpec
    dataset: DatasetManifest
    configuration: Mapping[str, object]
    folds: Sequence[Mapping[str, object]] | Mapping[str, object]
    metrics: Mapping[str, object]
    trade_summary: Mapping[str, object]
    cost_attribution: Mapping[str, object]
    regime_breakdown: Mapping[str, object]
    robustness: Mapping[str, object]
    monte_carlo: Mapping[str, object]
    statistical_tests: Mapping[str, object]
    multiple_testing: Mapping[str, object]
    warnings: Sequence[str]
    promotion_decision: str
    lineage: Mapping[str, object]

    def canonical(self) -> dict[str, object]:
        return _json_value(
            {
                "schema_version": "phase9-evidence.v1",
                "experiment_id": self.experiment.experiment_id,
                "experiment": self.experiment.canonical(),
                "dataset_manifest": {
                    "dataset_version": self.dataset.dataset_version,
                    "dataset_hash": self.dataset.dataset_hash,
                    "symbols": self.dataset.symbols,
                    "timeframe": self.dataset.timeframe.value,
                    "period": (self.dataset.period.start, self.dataset.period.end),
                    "adjustment_policy": self.dataset.adjustment_policy.value,
                    "source": self.dataset.source,
                    "quality_status": self.dataset.quality_status,
                },
                "configuration": self.configuration,
                "folds": self.folds,
                "performance_metrics": self.metrics,
                "trade_summary": self.trade_summary,
                "cost_attribution": self.cost_attribution,
                "regime_breakdown": self.regime_breakdown,
                "robustness": self.robustness,
                "monte_carlo": self.monte_carlo,
                "statistical_tests": self.statistical_tests,
                "multiple_testing": self.multiple_testing,
                "warnings": self.warnings,
                "promotion_decision": self.promotion_decision,
                "lineage": self.lineage,
            }
        )  # type: ignore[return-value]

    @property
    def package_hash(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()

    def to_json(self) -> str:
        return json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))


__all__ = ["EvidencePackage"]
