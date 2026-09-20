"""Strict adapter from a frozen Phase 9 experiment to the Phase 3 backtester."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from traderos.backtesting.engine import BacktestEngine
from traderos.backtesting.models import BacktestConfig, BacktestResult
from traderos.backtesting.strategies import Strategy
from traderos.data.bars import MarketBar
from traderos.features.models import FeatureObservation
from traderos.research.models import (
    DatasetManifest,
    ExperimentSpec,
    ResearchError,
    ResearchPlan,
    ResearchScope,
)
from traderos.research.registry import ExperimentRegistry


def execute_backtest(
    *,
    plan: ResearchPlan,
    spec: ExperimentSpec,
    config: BacktestConfig,
    bars: Sequence[MarketBar],
    dataset_bars: Sequence[MarketBar] | None = None,
    strategy: Strategy,
    features: Iterable[FeatureObservation] = (),
    oos_registry: ExperimentRegistry | None = None,
    oos_accessed_at: datetime | None = None,
) -> BacktestResult:
    """Run an existing causal backtest only after validating its research lineage.

    This function deliberately exposes no parameter search, ranking, or
    mutation API. It is equally usable for a frozen walk-forward configuration
    and for an explicitly non-selection-eligible locked OOS evaluation.
    """

    plan.validate_experiment(spec)
    if dataset_bars is None:
        raise ResearchError("research execution requires the verified parent dataset bars")
    if DatasetManifest.content_hash(dataset_bars) != spec.dataset.dataset_hash:
        raise ResearchError("parent dataset content differs from frozen dataset lineage")
    if config.experiment_id != spec.backtest_experiment_id:
        raise ResearchError("backtest configuration identity differs from experiment lineage")
    if config.dataset_version != spec.dataset.dataset_version:
        raise ResearchError("backtest dataset version differs from experiment lineage")
    if config.start != spec.period.start or config.end != spec.period.end:
        raise ResearchError("backtest period differs from experiment lineage")
    if config.strategy_id != spec.strategy_id or config.strategy_version != spec.strategy_version:
        raise ResearchError("backtest strategy differs from experiment lineage")
    if (
        strategy.strategy_id != spec.strategy_id
        or strategy.strategy_version != spec.strategy_version
    ):
        raise ResearchError("strategy object differs from frozen experiment lineage")
    declared_parameters = getattr(strategy, "parameters", None)
    if declared_parameters is None:
        if spec.parameters:
            raise ResearchError(
                "strategy parameters are unavailable for frozen experiment lineage"
            )
    else:
        actual_parameters = tuple(
            sorted((name, str(value)) for name, value in declared_parameters.items())
        )
        if actual_parameters != spec.parameters:
            raise ResearchError("strategy parameters differ from frozen experiment lineage")
    if spec.scope is ResearchScope.LOCKED_OUT_OF_SAMPLE:
        if oos_registry is None or oos_accessed_at is None:
            raise ResearchError("locked OOS evaluation requires a durable access registry")
        oos_registry.record_oos_access(experiment=spec, accessed_at=oos_accessed_at)
    return BacktestEngine(config).run(bars, strategy, features=features)


__all__ = ["execute_backtest"]
