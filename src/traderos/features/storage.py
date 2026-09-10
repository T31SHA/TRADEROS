"""Storage boundary for long-form feature observations.

Phase 2 provides an in-memory implementation for deterministic tests and local
research.  The interface is intentionally compatible with a future database
adapter; the feature engine does not know which storage implementation is used.
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Protocol

from traderos.data.errors import TimestampNormalizationError
from traderos.data.lineage import AdjustmentPolicy
from traderos.data.time import require_utc
from traderos.data.timeframes import Timeframe
from traderos.features.errors import FeatureValidationError
from traderos.features.models import FeatureObservation, FeatureSet
from traderos.features.validation import validate_feature_set

FeatureKey = tuple[
    str,
    Timeframe,
    datetime,
    str,
    int,
    str,
    AdjustmentPolicy,
    tuple[tuple[str, str], ...],
]


def _parameter_key(observation: FeatureObservation) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted((name, str(value)) for name, value in observation.lineage.parameters.items())
    )


def _key(observation: FeatureObservation, dataset_version: str) -> FeatureKey:
    return (
        observation.instrument.canonical_symbol,
        observation.timeframe,
        observation.observation_timestamp,
        observation.feature_name,
        observation.feature_version,
        dataset_version,
        observation.lineage.adjustment_policy,
        _parameter_key(observation),
    )


class FeatureStore(Protocol):
    """Provider-neutral feature persistence and bounded-query contract."""

    def upsert(self, feature_set: FeatureSet) -> int:
        """Insert or replace observations and return newly inserted count."""

    def query(
        self,
        *,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        dataset_version: str,
        adjustment_policy: AdjustmentPolicy,
        feature_keys: Iterable[tuple[str, int]] | None = None,
    ) -> tuple[FeatureObservation, ...]:
        """Return observations within an explicit bounded range."""


class InMemoryFeatureStore:
    """Idempotent reference store used by local research and integration tests."""

    def __init__(self) -> None:
        self._observations: dict[FeatureKey, FeatureObservation] = {}

    def upsert(self, feature_set: FeatureSet) -> int:
        validate_feature_set(feature_set)
        inserted = 0
        dataset_version = feature_set.context.source_dataset_version
        for observation in feature_set.observations:
            key = _key(observation, dataset_version)
            if key not in self._observations:
                inserted += 1
            self._observations[key] = observation
        return inserted

    def query(
        self,
        *,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        dataset_version: str,
        adjustment_policy: AdjustmentPolicy,
        feature_keys: Iterable[tuple[str, int]] | None = None,
    ) -> tuple[FeatureObservation, ...]:
        if start > end:
            raise FeatureValidationError("query start must not be after end")
        try:
            require_utc(start)
            require_utc(end)
        except TimestampNormalizationError as exc:
            raise FeatureValidationError(str(exc)) from exc
        requested = set(feature_keys) if feature_keys is not None else None
        result = [
            observation
            for observation in self._observations.values()
            if observation.instrument.canonical_symbol == symbol
            and observation.timeframe is timeframe
            and start <= observation.observation_timestamp <= end
            and observation.lineage.source_dataset_version == dataset_version
            and observation.lineage.adjustment_policy is adjustment_policy
            and (
                requested is None
                or (observation.feature_name, observation.feature_version) in requested
            )
        ]
        return tuple(
            sorted(
                result,
                key=lambda observation: (
                    observation.observation_timestamp,
                    observation.feature_name,
                    observation.feature_version,
                    _parameter_key(observation),
                ),
            )
        )

    def latest(
        self,
        *,
        symbol: str,
        timeframe: Timeframe,
        dataset_version: str,
        adjustment_policy: AdjustmentPolicy,
    ) -> FeatureObservation | None:
        candidates = self.query(
            symbol=symbol,
            timeframe=timeframe,
            start=datetime.min.replace(tzinfo=UTC),
            end=datetime.max.replace(tzinfo=UTC),
            dataset_version=dataset_version,
            adjustment_policy=adjustment_policy,
        )
        return candidates[-1] if candidates else None

    def __len__(self) -> int:
        return len(self._observations)


__all__ = ["FeatureStore", "InMemoryFeatureStore"]
