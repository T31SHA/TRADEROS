"""Deterministic causal feature computation over validated Phase 1 bars."""

from collections.abc import Iterable, Sequence
from datetime import datetime

from traderos.data.bars import MarketBar
from traderos.features.errors import FeatureComputationError, FeatureConfigurationError
from traderos.features.indicators import COMPUTERS
from traderos.features.models import (
    FeatureContext,
    FeatureLineage,
    FeatureObservation,
    FeatureRequest,
    FeatureSet,
    FeatureStatus,
    availability_timestamp,
)
from traderos.features.registry import FeatureRegistry, build_default_registry
from traderos.features.validation import validate_bar_series, validate_feature_set


def _in_range(timestamp: datetime, context: FeatureContext) -> bool:
    if context.start is not None and timestamp < context.start:
        return False
    return context.end is None or timestamp <= context.end


class FeatureEngine:
    """Compute registered feature definitions without provider or strategy coupling."""

    def __init__(self, registry: FeatureRegistry | None = None) -> None:
        self.registry = registry or build_default_registry()

    def compute_feature(
        self,
        bars: Sequence[MarketBar],
        request: FeatureRequest,
        context: FeatureContext,
    ) -> FeatureSet:
        validate_bar_series(bars, context)
        definition = self.registry.resolve(
            request.name,
            request.version,
            context.instrument.asset_class,
            context.timeframe,
        )
        try:
            computer = COMPUTERS[(definition.name, definition.version)]
        except KeyError as exc:
            raise FeatureConfigurationError(
                f"no controlled implementation exists for {definition.name}.v{definition.version}"
            ) from exc
        unknown_parameters = set(request.parameters) - definition.parameter_names
        if unknown_parameters:
            raise FeatureConfigurationError(
                f"unsupported parameters for {definition.name}.v{definition.version}: "
                f"{sorted(unknown_parameters)}"
            )
        parameters = {**definition.default_parameters, **request.parameters}
        try:
            computed = computer(bars, parameters)
        except FeatureComputationError:
            raise
        except (ArithmeticError, ValueError, TypeError) as exc:
            raise FeatureComputationError(
                f"failed to compute {definition.name}.v{definition.version}"
            ) from exc
        if len(computed.values) != len(bars):
            raise FeatureComputationError("feature implementation returned the wrong row count")

        observations: list[FeatureObservation] = []
        for index, (bar, value) in enumerate(zip(bars, computed.values, strict=True)):
            if not _in_range(bar.timestamp, context):
                continue
            if value is not None:
                status = FeatureStatus.VALUE
            elif index < computed.warmup_until:
                status = FeatureStatus.WARMUP
            elif index in computed.undefined_indices:
                status = FeatureStatus.UNDEFINED
            else:
                status = FeatureStatus.MISSING_INPUT
            available, decision = availability_timestamp(bar, definition, context.decision_lag)
            lineage = FeatureLineage(
                source_dataset_version=context.source_dataset_version,
                symbol=bar.symbol,
                asset_class=bar.asset_class,
                timeframe=bar.timeframe,
                adjustment_policy=bar.adjustment_policy,
                feature_name=definition.name,
                feature_version=definition.version,
                parameters=parameters,
                computation_version=definition.implementation_version,
                input_columns=definition.required_columns,
                dependencies=definition.dependencies,
                computed_at=context.computation_timestamp,
            )
            observations.append(
                FeatureObservation(
                    instrument=bar.instrument,
                    timeframe=bar.timeframe,
                    observation_timestamp=bar.timestamp,
                    availability_timestamp=available,
                    decision_timestamp=decision,
                    feature_name=definition.name,
                    feature_version=definition.version,
                    value=value,
                    status=status,
                    lineage=lineage,
                )
            )
        return validate_feature_set(FeatureSet(context=context, observations=tuple(observations)))

    def compute_feature_set(
        self,
        bars: Sequence[MarketBar],
        requests: Iterable[FeatureRequest],
        context: FeatureContext,
    ) -> FeatureSet:
        """Compute a long-form feature set with one lineage record per feature."""

        requests_tuple = tuple(requests)
        if not requests_tuple:
            raise FeatureConfigurationError("at least one feature request is required")
        keys = [(request.name, request.version) for request in requests_tuple]
        if len(set(keys)) != len(keys):
            raise FeatureConfigurationError("duplicate feature requests are not allowed")
        observations: list[FeatureObservation] = []
        for request in requests_tuple:
            observations.extend(self.compute_feature(bars, request, context).observations)
        return validate_feature_set(FeatureSet(context=context, observations=tuple(observations)))


def compute_feature(
    bars: Sequence[MarketBar],
    request: FeatureRequest,
    context: FeatureContext,
    registry: FeatureRegistry | None = None,
) -> FeatureSet:
    """Functional convenience wrapper around :class:`FeatureEngine`."""

    return FeatureEngine(registry).compute_feature(bars, request, context)


def compute_feature_set(
    bars: Sequence[MarketBar],
    requests: Iterable[FeatureRequest],
    context: FeatureContext,
    registry: FeatureRegistry | None = None,
) -> FeatureSet:
    """Functional convenience wrapper for multi-feature computation."""

    return FeatureEngine(registry).compute_feature_set(bars, requests, context)


__all__ = ["FeatureEngine", "compute_feature", "compute_feature_set"]
