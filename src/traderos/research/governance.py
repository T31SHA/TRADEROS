"""Immutable hypotheses, candidates, warnings, and research-family lineage."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from traderos.data.time import require_utc
from traderos.research.models import DatasetManifest, ResearchError


class CandidateStatus(StrEnum):
    DRAFT = "draft"
    RESEARCH = "research"
    VALIDATING = "validating"
    OOS_LOCKED = "oos_locked"
    PASSED_VALIDATION = "passed_validation"
    REJECTED = "rejected"
    PROMOTION_REVIEW = "promotion_review"
    PROMOTED = "promoted"
    RETIRED = "retired"


class ResearchWarning(StrEnum):
    LOW_SAMPLE_SIZE = "low_sample_size"
    HIGH_TRADE_AUTOCORRELATION = "high_trade_autocorrelation"
    PARAMETER_FRAGILITY = "parameter_fragility"
    COST_SENSITIVITY = "cost_sensitivity"
    REGIME_DEPENDENCE = "regime_dependence"
    OUT_OF_SAMPLE_DEGRADATION = "out_of_sample_degradation"
    MULTIPLE_TESTING_RISK = "multiple_testing_risk"
    SURVIVORSHIP_RISK = "survivorship_risk"
    DATA_QUALITY_RISK = "data_quality_risk"
    HIGH_TURNOVER = "high_turnover"
    EXTREME_CONCENTRATION = "extreme_concentration"
    SINGLE_PERIOD_DEPENDENCE = "single_period_dependence"
    SINGLE_ASSET_DEPENDENCE = "single_asset_dependence"


@dataclass(frozen=True)
class ResearchHypothesis:
    """A predeclared proposition; it deliberately contains no performance claim."""

    hypothesis_id: str
    name: str
    description: str
    asset_class: str
    instrument_scope: tuple[str, ...]
    timeframe_scope: tuple[str, ...]
    economic_rationale: str
    expected_market_regime: str
    candidate_strategy: str
    candidate_parameters: tuple[tuple[str, str], ...]
    feature_dependencies: tuple[str, ...]
    cost_assumptions: str
    risk_assumptions: str
    research_owner: str
    created_at: datetime
    version: str

    def __post_init__(self) -> None:
        require_utc(self.created_at)
        required = (
            self.hypothesis_id,
            self.name,
            self.description,
            self.asset_class,
            self.economic_rationale,
            self.expected_market_regime,
            self.candidate_strategy,
            self.cost_assumptions,
            self.risk_assumptions,
            self.research_owner,
            self.version,
        )
        if any(not value.strip() for value in required):
            raise ResearchError("hypothesis fields must not be blank")
        if not self.instrument_scope or not self.timeframe_scope:
            raise ResearchError("hypothesis must declare instrument and timeframe scopes")
        names = [name for name, _ in self.candidate_parameters]
        if len(names) != len(set(names)) or any(not name.strip() for name in names):
            raise ResearchError("hypothesis parameter names must be unique and non-blank")

    @classmethod
    def create(
        cls, *, candidate_parameters: Mapping[str, object], **kwargs: object
    ) -> ResearchHypothesis:
        return cls(
            candidate_parameters=tuple(
                sorted((key, str(value)) for key, value in candidate_parameters.items())
            ),
            **kwargs,  # type: ignore[arg-type]
        )

    @property
    def identity(self) -> str:
        payload = {
            "hypothesis_id": self.hypothesis_id,
            "version": self.version,
            "parameters": self.candidate_parameters,
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return "hypothesis-" + digest[:24]


@dataclass(frozen=True)
class StrategyCandidate:
    """A versioned candidate and its full research-family lineage."""

    family_id: str
    hypothesis_id: str
    strategy_id: str
    strategy_version: str
    parameters: tuple[tuple[str, str], ...]
    feature_versions: tuple[tuple[str, int], ...]
    regime_configuration_id: str | None
    fusion_configuration_id: str | None
    risk_configuration_id: str | None
    dataset_identity: str
    creation_experiment_id: str
    parent_candidate_id: str | None
    status: CandidateStatus = CandidateStatus.DRAFT

    def __post_init__(self) -> None:
        required = (
            self.family_id,
            self.hypothesis_id,
            self.strategy_id,
            self.strategy_version,
            self.dataset_identity,
            self.creation_experiment_id,
        )
        if any(not value.strip() for value in required):
            raise ResearchError("candidate lineage fields must not be blank")
        names = [name for name, _ in self.parameters]
        if len(names) != len(set(names)) or any(not name.strip() for name in names):
            raise ResearchError("candidate parameter names must be unique and non-blank")

    @classmethod
    def create(
        cls,
        *,
        parameters: Mapping[str, object],
        dataset: DatasetManifest,
        **kwargs: object,
    ) -> StrategyCandidate:
        return cls(
            parameters=tuple(sorted((key, str(value)) for key, value in parameters.items())),
            dataset_identity=dataset.dataset_hash,
            **kwargs,  # type: ignore[arg-type]
        )

    @property
    def candidate_id(self) -> str:
        payload = {
            "family_id": self.family_id,
            "hypothesis_id": self.hypothesis_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "parameters": self.parameters,
            "feature_versions": self.feature_versions,
            "regime": self.regime_configuration_id,
            "fusion": self.fusion_configuration_id,
            "risk": self.risk_configuration_id,
            "dataset": self.dataset_identity,
            "parent": self.parent_candidate_id,
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return "candidate-" + digest[:24]

    def transition(self, status: CandidateStatus) -> StrategyCandidate:
        """Return a new record.  Locked candidates cannot be silently re-opened."""
        allowed = {
            CandidateStatus.DRAFT: {CandidateStatus.RESEARCH, CandidateStatus.RETIRED},
            CandidateStatus.RESEARCH: {
                CandidateStatus.VALIDATING,
                CandidateStatus.REJECTED,
                CandidateStatus.RETIRED,
            },
            CandidateStatus.VALIDATING: {
                CandidateStatus.OOS_LOCKED,
                CandidateStatus.REJECTED,
            },
            CandidateStatus.OOS_LOCKED: {
                CandidateStatus.PASSED_VALIDATION,
                CandidateStatus.REJECTED,
            },
            CandidateStatus.PASSED_VALIDATION: {
                CandidateStatus.PROMOTION_REVIEW,
                CandidateStatus.RETIRED,
            },
            CandidateStatus.PROMOTION_REVIEW: {
                CandidateStatus.PROMOTED,
                CandidateStatus.REJECTED,
                CandidateStatus.RETIRED,
            },
            CandidateStatus.PROMOTED: {CandidateStatus.RETIRED},
            CandidateStatus.REJECTED: {CandidateStatus.RETIRED},
            CandidateStatus.RETIRED: set(),
        }
        if status not in allowed[self.status]:
            raise ResearchError(
                f"invalid candidate transition {self.status.value} -> {status.value}"
            )
        return StrategyCandidate(**{**self.__dict__, "status": status})


__all__ = ["CandidateStatus", "ResearchHypothesis", "ResearchWarning", "StrategyCandidate"]
