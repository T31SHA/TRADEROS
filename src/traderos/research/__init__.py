"""Phase 9 deterministic research governance, analysis, and experiment lineage."""

from traderos.research.analysis import (
    DecisionEvidence,
    MonteCarloSummary,
    benjamini_hochberg_adjusted_p_values,
    benjamini_yekutieli_adjusted_p_values,
    classify_evidence,
    moving_block_bootstrap_mean,
    trade_path_monte_carlo,
)
from traderos.research.dataset import (
    DatasetEligibilityReport,
    DatasetQualification,
    DatasetQualificationStatus,
    QualificationPolicy,
    ResearchDatasetEligibility,
    assess_empirical_eligibility,
    qualify_dataset,
)
from traderos.research.evidence import EvidencePackage
from traderos.research.execution import execute_backtest
from traderos.research.governance import (
    CandidateStatus,
    ResearchHypothesis,
    ResearchWarning,
    StrategyCandidate,
)
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
from traderos.research.promotion import (
    PromotionDecision,
    PromotionEvidence,
    PromotionPolicy,
    evaluate_promotion,
)
from traderos.research.registry import ExperimentRegistry, RegisteredExperiment
from traderos.research.replay import (
    CanonicalHistoricalReplay,
    ReplayAuditEvent,
    ReplayConfigurationError,
)
from traderos.research.service import ResearchValidationEngine
from traderos.research.validation import ChronologicalValidationProtocol, ValidationFold

__all__ = [
    "CandidateStatus",
    "CanonicalHistoricalReplay",
    "CostScenario",
    "ChronologicalValidationProtocol",
    "DatasetQualification",
    "DatasetQualificationStatus",
    "DatasetEligibilityReport",
    "DatasetManifest",
    "DecisionEvidence",
    "EvidencePackage",
    "ExperimentRegistry",
    "ExperimentResult",
    "ExperimentSpec",
    "OosContaminationError",
    "RegisteredExperiment",
    "ResearchDecision",
    "ResearchError",
    "ResearchDatasetEligibility",
    "ResearchPlan",
    "ResearchScope",
    "ResearchSplit",
    "TemporalRange",
    "WalkForwardConfig",
    "WalkForwardFold",
    "WalkForwardMode",
    "MonteCarloSummary",
    "PromotionDecision",
    "PromotionEvidence",
    "PromotionPolicy",
    "QualificationPolicy",
    "ResearchHypothesis",
    "ResearchValidationEngine",
    "ResearchWarning",
    "ReplayAuditEvent",
    "ReplayConfigurationError",
    "StrategyCandidate",
    "ValidationFold",
    "assess_empirical_eligibility",
    "benjamini_hochberg_adjusted_p_values",
    "benjamini_yekutieli_adjusted_p_values",
    "classify_evidence",
    "evaluate_promotion",
    "execute_backtest",
    "moving_block_bootstrap_mean",
    "qualify_dataset",
    "trade_path_monte_carlo",
]
