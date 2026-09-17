"""Configured, conservative Phase 9 promotion recommendations.

The output is a research decision only.  It cannot authorize a broker, live
trading, deployment, or even automatic paper trading.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from traderos.research.governance import ResearchWarning
from traderos.research.models import ResearchError


class PromotionDecision(StrEnum):
    REJECT = "reject"
    HOLD = "hold"
    PROMOTION_REVIEW = "promotion_review"
    PROMOTE_TO_PAPER = "promote_to_paper"


@dataclass(frozen=True)
class PromotionPolicy:
    """Versioned evidence requirements, explicitly supplied by the researcher."""

    policy_id: str
    version: str
    justification: str
    minimum_trade_count: int
    allowed_adjusted_p_value: float | None = None
    severe_warnings: tuple[ResearchWarning, ...] = (
        ResearchWarning.DATA_QUALITY_RISK,
        ResearchWarning.SURVIVORSHIP_RISK,
        ResearchWarning.MULTIPLE_TESTING_RISK,
    )

    def __post_init__(self) -> None:
        if not self.policy_id.strip() or not self.version.strip() or not self.justification.strip():
            raise ResearchError("promotion policy identity and justification must not be blank")
        if self.minimum_trade_count < 1:
            raise ResearchError("promotion policy minimum trade count must be positive")
        if self.allowed_adjusted_p_value is not None and not 0 < self.allowed_adjusted_p_value <= 1:
            raise ResearchError("promotion policy adjusted p-value must lie in (0, 1]")


@dataclass(frozen=True)
class PromotionEvidence:
    dataset_qualified: bool
    oos_locked_and_pristine: bool
    risk_policy_compatible: bool
    walk_forward_positive: bool | None
    oos_cost_adjusted_positive: bool | None
    robustness_survives: bool | None
    parameter_stable: bool | None
    regime_stable: bool | None
    adjusted_p_value: float | None
    trade_count: int
    warnings: tuple[ResearchWarning, ...] = ()


def evaluate_promotion(policy: PromotionPolicy, evidence: PromotionEvidence) -> PromotionDecision:
    """Return a repeatable recommendation without inventing universal thresholds."""

    if not evidence.dataset_qualified or not evidence.oos_locked_and_pristine:
        return PromotionDecision.REJECT
    has_severe_warning = any(item in policy.severe_warnings for item in evidence.warnings)
    if not evidence.risk_policy_compatible or has_severe_warning:
        return PromotionDecision.REJECT
    if evidence.trade_count < policy.minimum_trade_count:
        return PromotionDecision.HOLD
    required = (
        evidence.walk_forward_positive,
        evidence.oos_cost_adjusted_positive,
        evidence.robustness_survives,
        evidence.parameter_stable,
        evidence.regime_stable,
    )
    if any(value is False for value in required):
        return PromotionDecision.REJECT
    if any(value is None for value in required):
        return PromotionDecision.HOLD
    if policy.allowed_adjusted_p_value is not None:
        if evidence.adjusted_p_value is None:
            return PromotionDecision.HOLD
        if evidence.adjusted_p_value > policy.allowed_adjusted_p_value:
            return PromotionDecision.REJECT
    return PromotionDecision.PROMOTION_REVIEW


__all__ = ["PromotionDecision", "PromotionEvidence", "PromotionPolicy", "evaluate_promotion"]
