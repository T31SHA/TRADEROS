"""Phase 9 statistical reporting and evidence-classification tests."""

from decimal import Decimal

import pytest

from traderos.research.analysis import (
    DecisionEvidence,
    benjamini_yekutieli_adjusted_p_values,
    classify_evidence,
    decision_matrix,
    moving_block_bootstrap_mean,
    net_after_costs,
    outlier_dependence,
)
from traderos.research.models import ResearchDecision, ResearchError


def _evidence(**changes: object) -> DecisionEvidence:
    values: dict[str, object] = {
        "data_available": True,
        "data_integrity": True,
        "locked_oos_pristine": True,
        "sample_adequate": True,
        "net_expectancy": Decimal("1"),
        "net_oos_return": 0.10,
        "benchmark_excess_return": 0.02,
        "cost_survives": True,
        "slippage_survives": True,
        "parameter_robust": True,
        "walk_forward_consistent": True,
        "multiple_testing_supported": True,
        "regime_concentrated": False,
        "outlier_dependent": False,
    }
    values.update(changes)
    return DecisionEvidence(**values)  # type: ignore[arg-type]


def test_moving_block_bootstrap_is_deterministic_and_preserves_insufficient_sample_state() -> None:
    values = (0.02, -0.01, 0.03, 0.01, -0.02, 0.04)
    first = moving_block_bootstrap_mean(values, replications=200, block_size=2, seed=9)
    second = moving_block_bootstrap_mean(values, replications=200, block_size=2, seed=9)
    insufficient = moving_block_bootstrap_mean((0.01,), replications=20, block_size=2, seed=9)

    assert first == second
    assert first.mean == pytest.approx(sum(values) / len(values))
    assert first.lower is not None and first.upper is not None
    assert insufficient.mean is None
    assert insufficient.probability_mean_nonpositive is None
    with pytest.raises(ResearchError, match="finite"):
        moving_block_bootstrap_mean((float("nan"),), replications=10, block_size=1, seed=1)
    with pytest.raises(ResearchError, match="positive"):
        moving_block_bootstrap_mean((0.1, 0.2), replications=0, block_size=1, seed=1)
    with pytest.raises(ResearchError, match="confidence"):
        moving_block_bootstrap_mean((0.1, 0.2), replications=10, block_size=1, seed=1, confidence=1)


def test_multiple_testing_adjustment_is_conservative_and_validates_inputs() -> None:
    adjusted = benjamini_yekutieli_adjusted_p_values({"a": 0.01, "b": 0.02, "c": 0.8})

    assert adjusted["a"] >= 0.01
    assert adjusted["b"] >= 0.02
    assert adjusted["c"] >= 0.8
    assert adjusted["a"] <= adjusted["b"] <= adjusted["c"]
    with pytest.raises(ResearchError):
        benjamini_yekutieli_adjusted_p_values({"": 0.1})
    assert benjamini_yekutieli_adjusted_p_values({}) == {}


def test_outlier_and_cost_analysis_preserve_negative_results() -> None:
    outliers = outlier_dependence((Decimal("10"), Decimal("2"), Decimal("-1")))

    assert outliers.total == Decimal("11")
    assert outliers.top_one_contribution == Decimal("10")
    assert outliers.total_without_top_one == Decimal("1")
    assert net_after_costs(Decimal("10"), Decimal("3")) == Decimal("7")
    assert net_after_costs(Decimal("10"), Decimal("8")) < net_after_costs(
        Decimal("10"), Decimal("3")
    )
    with pytest.raises(ResearchError):
        net_after_costs(Decimal("1"), Decimal("-0.01"))
    assert outlier_dependence(()).top_one_contribution is None
    with pytest.raises(ResearchError, match="finite"):
        outlier_dependence((Decimal("NaN"),))


def test_evidence_classifier_requires_data_and_does_not_use_magic_ratio_thresholds() -> None:
    assert classify_evidence(_evidence(data_available=False)) is ResearchDecision.INVALID
    assert classify_evidence(_evidence(sample_adequate=False)) is ResearchDecision.CONDITIONAL
    assert classify_evidence(_evidence(net_expectancy=Decimal("-0.01"))) is ResearchDecision.KILL
    assert classify_evidence(_evidence(cost_survives=False)) is ResearchDecision.KILL
    assert classify_evidence(_evidence(parameter_robust=None)) is ResearchDecision.CONDITIONAL
    assert classify_evidence(_evidence()) is ResearchDecision.PASS

    rows = decision_matrix(_evidence(net_oos_return=None))
    assert next(row for row in rows if row.dimension == "Net OOS return").result == "UNAVAILABLE"
