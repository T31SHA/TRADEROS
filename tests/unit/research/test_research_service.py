"""Service-level failure modes preserve immutable Phase 9 boundaries."""

from types import SimpleNamespace

import pytest

from traderos.research import ResearchError, ResearchScope, ResearchValidationEngine
from traderos.research.validation import ChronologicalValidationProtocol


def test_service_rejects_mismatched_walk_forward_results_and_selects_fdr_method() -> None:
    engine = ResearchValidationEngine()
    with pytest.raises(ResearchError, match="exactly one"):
        engine.run_walk_forward((), (SimpleNamespace(),))
    assert engine.run_statistical_validation({"a": 0.01}, arbitrary_dependence=False) == {"a": 0.01}
    assert engine.run_statistical_validation({"a": 0.01}, arbitrary_dependence=True) == {"a": 0.01}
    assert engine.run_monte_carlo((0.01, -0.01), simulations=4, seed=1, block_size=1).seed == 1
    fold = SimpleNamespace(fold_index=4)
    result = SimpleNamespace(metrics=SimpleNamespace(total_return=0.02))
    assert engine.run_walk_forward((fold,), (result,))["worst_total_return"] == 0.02
    with pytest.raises(ResearchError, match="unavailable"):
        engine.run_walk_forward(
            (fold,), (SimpleNamespace(metrics=SimpleNamespace(total_return=None)),)
        )


def test_service_blocks_candidate_mismatch_and_oos_changes_without_execution() -> None:
    engine = ResearchValidationEngine()
    qualified = SimpleNamespace(require_qualified=lambda: None)
    with pytest.raises(ResearchError, match="predeclared hypothesis"):
        engine.create_candidate(
            dataset=qualified,
            hypothesis=SimpleNamespace(candidate_strategy="expected", hypothesis_id="h"),
            family_id="family",
            strategy_id="changed",
            strategy_version="1",
            parameters={},
            feature_versions=(),
            regime_configuration_id=None,
            fusion_configuration_id=None,
            risk_configuration_id=None,
            creation_experiment_id="creation",
        )
    with pytest.raises(ResearchError, match="cannot freeze"):
        engine.lock_oos(
            development=SimpleNamespace(scope=ResearchScope.LOCKED_OUT_OF_SAMPLE),
            oos=SimpleNamespace(scope=ResearchScope.LOCKED_OUT_OF_SAMPLE),
        )
    with pytest.raises(ResearchError, match="requires a locked"):
        engine.lock_oos(
            development=SimpleNamespace(scope=ResearchScope.DEVELOPMENT),
            oos=SimpleNamespace(scope=ResearchScope.DEVELOPMENT),
        )
    development = SimpleNamespace(
        scope=ResearchScope.DEVELOPMENT,
        dataset="dataset",
        strategy_id="s",
        strategy_version="1",
        parameters=(),
        feature_versions=(),
        regime_configuration_id=None,
        fusion_configuration_id=None,
        risk_policy_configuration_id=None,
        cost_scenario_id="base",
        code_version="code",
        candidate_id="candidate",
        experiment_id="development",
    )
    changed = SimpleNamespace(
        **{
            **development.__dict__,
            "scope": ResearchScope.LOCKED_OUT_OF_SAMPLE,
            "parameters": (("x", "2"),),
            "frozen_from_experiment_id": None,
        }
    )
    with pytest.raises(ResearchError, match="changed research"):
        engine.lock_oos(development=development, oos=changed)
    wrong_parent = SimpleNamespace(
        **{
            **development.__dict__,
            "scope": ResearchScope.LOCKED_OUT_OF_SAMPLE,
            "frozen_from_experiment_id": "other",
        }
    )
    with pytest.raises(ResearchError, match="different development"):
        engine.lock_oos(development=development, oos=wrong_parent)


def test_validation_protocol_rejects_invalid_configuration() -> None:
    from datetime import timedelta

    with pytest.raises(ResearchError, match="positive"):
        ChronologicalValidationProtocol(
            "protocol",
            "1",
            timedelta(0),
            timedelta(hours=1),
            timedelta(hours=1),
            timedelta(hours=1),
        )
    with pytest.raises(ResearchError, match="non-negative"):
        ChronologicalValidationProtocol(
            "protocol",
            "1",
            timedelta(hours=1),
            timedelta(hours=1),
            timedelta(hours=1),
            timedelta(hours=1),
            fold_count=0,
        )
