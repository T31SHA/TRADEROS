"""Strict adapter from a frozen Phase 9 experiment to the Phase 3 backtester."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from traderos.backtesting.engine import BacktestEngine
from traderos.backtesting.models import BacktestConfig, BacktestResult
from traderos.backtesting.strategies import Strategy
from traderos.data.bars import MarketBar
from traderos.features.models import FeatureObservation
from traderos.research.models import ExperimentSpec, ResearchError, ResearchPlan


def execute_backtest(
    *,
    plan: ResearchPlan,
    spec: ExperimentSpec,
    config: BacktestConfig,
    bars: Sequence[MarketBar],
    strategy: Strategy,
    features: Iterable[FeatureObservation] = (),
) -> BacktestResult:
    """Run an existing causal backtest only after validating its research lineage.

    This function deliberately exposes no parameter search, ranking, or
    mutation API. It is equally usable for a frozen walk-forward configuration
    and for an explicitly non-selection-eligible locked OOS evaluation.
    """

    plan.validate_experiment(spec)
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
    return BacktestEngine(config).run(bars, strategy, features=features)


__all__ = ["execute_backtest"]
