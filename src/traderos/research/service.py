"""Small deterministic orchestration surface for the Phase 9 lifecycle.

It composes immutable artifacts and calls the authoritative Phase 3 simulator.
It intentionally offers no optimizer, arbitrary code loader, broker path, or
automatic deployment operation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, replace
from datetime import datetime

from traderos.backtesting.models import BacktestConfig, BacktestResult
from traderos.backtesting.strategies import Strategy
from traderos.data.bars import MarketBar
from traderos.features.models import FeatureObservation
from traderos.research.analysis import (
    MonteCarloSummary,
    benjamini_hochberg_adjusted_p_values,
    benjamini_yekutieli_adjusted_p_values,
    trade_path_monte_carlo,
)
from traderos.research.dataset import (
    DatasetQualification,
    QualificationPolicy,
    qualify_dataset,
)
from traderos.research.evidence import EvidencePackage
from traderos.research.execution import execute_backtest
from traderos.research.governance import ResearchHypothesis, StrategyCandidate
from traderos.research.models import (
    DatasetManifest,
    ExperimentSpec,
    ResearchError,
    ResearchPlan,
    ResearchScope,
)
from traderos.research.promotion import (
    PromotionDecision,
    PromotionEvidence,
    PromotionPolicy,
    evaluate_promotion,
)
from traderos.research.registry import ExperimentRegistry
from traderos.research.validation import ValidationFold


class ResearchValidationEngine:
    """Coordinate validation while preserving Phase 3 execution ownership."""

    def create_hypothesis(self, **kwargs: object) -> ResearchHypothesis:
        return ResearchHypothesis.create(**kwargs)  # type: ignore[arg-type]

    def qualify_dataset(
        self,
        *,
        manifest: DatasetManifest,
        bars: Sequence[MarketBar],
        policy: QualificationPolicy,
        checked_at: datetime,
    ) -> DatasetQualification:
        return qualify_dataset(manifest=manifest, bars=bars, policy=policy, checked_at=checked_at)

    def create_candidate(
        self,
        *,
        dataset: DatasetQualification,
        hypothesis: ResearchHypothesis,
        family_id: str,
        strategy_id: str,
        strategy_version: str,
        parameters: Mapping[str, object],
        feature_versions: tuple[tuple[str, int], ...],
        regime_configuration_id: str | None,
        fusion_configuration_id: str | None,
        risk_configuration_id: str | None,
        creation_experiment_id: str,
        parent_candidate_id: str | None = None,
    ) -> StrategyCandidate:
        dataset.require_qualified()
        if strategy_id != hypothesis.candidate_strategy:
            raise ResearchError("candidate strategy differs from its predeclared hypothesis")
        return StrategyCandidate.create(
            dataset=dataset.manifest,
            family_id=family_id,
            hypothesis_id=hypothesis.hypothesis_id,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            parameters=parameters,
            feature_versions=feature_versions,
            regime_configuration_id=regime_configuration_id,
            fusion_configuration_id=fusion_configuration_id,
            risk_configuration_id=risk_configuration_id,
            creation_experiment_id=creation_experiment_id,
            parent_candidate_id=parent_candidate_id,
        )

    def create_experiment(
        self,
        *,
        plan: ResearchPlan,
        dataset: DatasetQualification,
        candidate: StrategyCandidate,
        config: BacktestConfig,
        scope: ResearchScope,
        code_version: str,
        cost_scenario_id: str,
        random_seed: int | None,
        selection_eligible: bool,
        frozen_from_experiment_id: str | None = None,
    ) -> ExperimentSpec:
        dataset.require_qualified()
        if candidate.dataset_identity != plan.dataset.dataset_hash:
            raise ResearchError("candidate dataset differs from research plan")
        if config.dataset_version != plan.dataset.dataset_version:
            raise ResearchError("backtest configuration dataset differs from research plan")
        if (
            config.strategy_id != candidate.strategy_id
            or config.strategy_version != candidate.strategy_version
        ):
            raise ResearchError("backtest configuration strategy differs from candidate")
        spec = ExperimentSpec(
            dataset=plan.dataset,
            split_id=plan.split.split_id,
            scope=scope,
            period=(
                plan.split.development
                if scope is not ResearchScope.LOCKED_OUT_OF_SAMPLE
                else plan.split.locked_out_of_sample
            ),
            strategy_id=candidate.strategy_id,
            strategy_version=candidate.strategy_version,
            parameters=candidate.parameters,
            feature_versions=candidate.feature_versions,
            regime_configuration_id=candidate.regime_configuration_id,
            fusion_configuration_id=candidate.fusion_configuration_id,
            risk_policy_configuration_id=candidate.risk_configuration_id,
            backtest_experiment_id=config.experiment_id,
            cost_scenario_id=cost_scenario_id,
            code_version=code_version,
            random_seed=random_seed,
            selection_eligible=selection_eligible,
            frozen_from_experiment_id=frozen_from_experiment_id,
            research_family_id=candidate.family_id,
            candidate_id=candidate.candidate_id,
        )
        plan.validate_experiment(spec)
        return spec

    def lock_oos(
        self,
        *,
        development: ExperimentSpec,
        oos: ExperimentSpec,
    ) -> ExperimentSpec:
        """Validate that OOS is a new, frozen evaluation rather than a mutation."""
        if development.scope is ResearchScope.LOCKED_OUT_OF_SAMPLE:
            raise ResearchError("a locked OOS experiment cannot freeze another OOS experiment")
        if oos.scope is not ResearchScope.LOCKED_OUT_OF_SAMPLE:
            raise ResearchError("OOS lock requires a locked out-of-sample experiment")
        pairs = (
            (development.dataset, oos.dataset),
            (development.strategy_id, oos.strategy_id),
            (development.strategy_version, oos.strategy_version),
            (development.parameters, oos.parameters),
            (development.feature_versions, oos.feature_versions),
            (development.regime_configuration_id, oos.regime_configuration_id),
            (development.fusion_configuration_id, oos.fusion_configuration_id),
            (development.risk_policy_configuration_id, oos.risk_policy_configuration_id),
            (development.cost_scenario_id, oos.cost_scenario_id),
            (development.code_version, oos.code_version),
            (development.candidate_id, oos.candidate_id),
        )
        if any(left != right for left, right in pairs):
            raise ResearchError("OOS lock rejects a changed research-defining input")
        if oos.frozen_from_experiment_id not in (None, development.experiment_id):
            raise ResearchError("OOS experiment is frozen from a different development experiment")
        return replace(
            oos,
            frozen_from_experiment_id=development.experiment_id,
            selection_eligible=False,
        )

    def run_backtest(
        self,
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
        return execute_backtest(
            plan=plan,
            spec=spec,
            config=config,
            bars=bars,
            dataset_bars=dataset_bars,
            strategy=strategy,
            features=features,
            oos_registry=oos_registry,
            oos_accessed_at=oos_accessed_at,
        )

    def run_walk_forward(
        self, folds: Sequence[ValidationFold], results: Sequence[BacktestResult]
    ) -> Mapping[str, object]:
        """Aggregate already executed chronological folds; selection stays external."""
        if len(folds) != len(results):
            raise ResearchError("each walk-forward fold requires exactly one Phase 3 result")
        fold_indices = tuple(fold.fold_index for fold in folds)
        if len(set(fold_indices)) != len(fold_indices) or fold_indices != tuple(
            sorted(fold_indices)
        ):
            raise ResearchError("walk-forward folds must be unique and chronological")
        test_periods = tuple((fold.test.start, fold.test.end) for fold in folds)
        if any(
            left_end > right_start
            for (_, left_end), (right_start, _) in zip(
                test_periods, test_periods[1:], strict=False
            )
        ):
            raise ResearchError(
                "walk-forward test periods must be chronological and non-overlapping"
            )
        for fold, result in zip(folds, results, strict=True):
            if result.config.start != fold.test.start or result.config.end != fold.test.end:
                raise ResearchError("walk-forward result period differs from its fold")
        values = tuple(item.metrics.total_return for item in results)
        if any(value is None for value in values):
            raise ResearchError("walk-forward results contain unavailable total returns")
        finite = tuple(float(value) for value in values if value is not None)
        return {
            "fold_count": len(folds),
            "fold_ids": tuple(fold.fold_index for fold in folds),
            "total_returns": finite,
            "mean_total_return": sum(finite) / len(finite) if finite else None,
            "worst_total_return": min(finite) if finite else None,
        }

    def run_monte_carlo(
        self, returns: Sequence[float], *, simulations: int, seed: int, block_size: int
    ) -> MonteCarloSummary:
        return trade_path_monte_carlo(
            returns, simulations=simulations, seed=seed, block_size=block_size
        )

    def run_statistical_validation(
        self, p_values: Mapping[str, float], *, arbitrary_dependence: bool
    ) -> Mapping[str, float]:
        return (
            benjamini_yekutieli_adjusted_p_values(p_values)
            if arbitrary_dependence
            else benjamini_hochberg_adjusted_p_values(p_values)
        )

    def build_evidence_package(
        self,
        *,
        spec: ExperimentSpec,
        configuration: Mapping[str, object],
        result: BacktestResult,
        promotion: PromotionDecision,
        warnings: Sequence[str] = (),
        additional: Mapping[str, Mapping[str, object]] | None = None,
    ) -> EvidencePackage:
        if result.experiment_id != spec.backtest_experiment_id:
            raise ResearchError("evidence result identity differs from experiment lineage")
        if result.config.experiment_id != spec.backtest_experiment_id:
            raise ResearchError("evidence result configuration differs from experiment lineage")
        extras = additional or {}
        metrics = asdict(result.metrics)
        cost = {
            "commissions_and_fees": result.metrics.total_fees,
            "net_equity": result.equity_curve[-1].equity if result.equity_curve else None,
            "gross_pnl_unavailable": (
                "Phase 3 ledger does not separately persist spread/slippage attribution"
            ),
        }
        return EvidencePackage(
            experiment=spec,
            dataset=spec.dataset,
            configuration=configuration,
            folds=extras.get("folds", {}),
            metrics=metrics,
            trade_summary={
                "trade_count": len(result.trades),
                "fill_count": len(result.fills),
            },
            cost_attribution=cost,
            regime_breakdown=extras.get("regime_breakdown", {}),
            robustness=extras.get("robustness", {}),
            monte_carlo=extras.get("monte_carlo", {}),
            statistical_tests=extras.get("statistical_tests", {}),
            multiple_testing=extras.get("multiple_testing", {}),
            warnings=tuple(warnings),
            promotion_decision=promotion.value,
            lineage={
                "research_family_id": spec.research_family_id,
                "candidate_id": spec.candidate_id,
            },
        )

    def evaluate_promotion(
        self, policy: PromotionPolicy, evidence: PromotionEvidence
    ) -> PromotionDecision:
        return evaluate_promotion(policy, evidence)


__all__ = ["ResearchValidationEngine"]
