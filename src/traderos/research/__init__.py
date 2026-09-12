"""Phase 9 deterministic research governance, analysis, and experiment lineage."""

from traderos.research.analysis import (
    DecisionEvidence,
    benjamini_yekutieli_adjusted_p_values,
    classify_evidence,
    moving_block_bootstrap_mean,
)
from traderos.research.execution import execute_backtest
from traderos.research.models import (
    CostScenario,
    DatasetManifest,
    ExperimentResult,
    ExperimentSpec,
    OosContaminationError,
    ResearchDecision,
    ResearchError,
    ResearchPlan,
    ResearchScope,
    ResearchSplit,
    TemporalRange,
    WalkForwardConfig,
    WalkForwardFold,
    WalkForwardMode,
)
from traderos.research.registry import ExperimentRegistry, RegisteredExperiment

__all__ = [
    "CostScenario",
    "DatasetManifest",
    "DecisionEvidence",
    "ExperimentRegistry",
    "ExperimentResult",
    "ExperimentSpec",
    "OosContaminationError",
    "RegisteredExperiment",
    "ResearchDecision",
    "ResearchError",
    "ResearchPlan",
    "ResearchScope",
    "ResearchSplit",
    "TemporalRange",
    "WalkForwardConfig",
    "WalkForwardFold",
    "WalkForwardMode",
    "benjamini_yekutieli_adjusted_p_values",
    "classify_evidence",
    "execute_backtest",
    "moving_block_bootstrap_mean",
]
