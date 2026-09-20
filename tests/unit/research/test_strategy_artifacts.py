"""Strategy artifact canonicalization and pure lifecycle invariants."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from traderos.research.models import ResearchDecision, ResearchScope, TemporalRange
from traderos.research.strategy_lifecycle import (
    ActorCapability,
    ApprovalRecord,
    EvaluationDecision,
    EvidenceAttachment,
    EvidenceOosStatus,
    FeatureDefinition,
    LifecyclePolicy,
    RegisteredImplementation,
    ResearchReference,
    StrategyArtifact,
    StrategyHealth,
    StrategyParameter,
    StrategyStage,
    StrategyState,
    TrustedActor,
    evaluate_current_eligibility,
    evaluate_transition,
)

BASE = datetime(2024, 1, 2, tzinfo=UTC)


def artifact(*, version: str = "1", author: str = "researcher") -> StrategyArtifact:
    period = TemporalRange(BASE, BASE + timedelta(days=1))
    reference = ResearchReference("exp-1", "dataset-hash", ResearchScope.DEVELOPMENT, period)
    return StrategyArtifact(
        strategy_id="fixture_strategy",
        strategy_version=version,
        family_id="fixture-family",
        author=author,
        implementation=RegisteredImplementation("always_flat", "1"),
        instruments=("EUR/USD",),
        timeframes=("1h",),
        parameters=(
            StrategyParameter("lookback", 3),
            StrategyParameter("threshold", Decimal("1.0")),
        ),
        features=(FeatureDefinition("return", "1", (StrategyParameter("window", 3),)),),
        training_reference=reference,
        validation_reference=None,
        oos_reference=None,
        forecast_semantics="directional signal; no executable order authority",
        holding_period="1h",
        turnover_assumption=Decimal("0.5"),
        cost_model_reference=None,
        capacity_model_reference=None,
        regime_dependencies=("regime-v1",),
        risk_dependencies=("risk-firewall-v1",),
        known_failure_modes=("thin sample",),
        code_identity="git:fixture-code",
        dataset_identity="dataset-hash",
        configuration_identity=None,
        environment_reference="python-3.12",
        unavailable_evidence=("empirical_oos",),
    )


def evidence(artifact_hash: str, *, fixture: bool = False) -> EvidenceAttachment:
    return EvidenceAttachment(
        evidence_id="evidence-1",
        artifact_hash=artifact_hash,
        dataset_hashes=("dataset-hash",),
        experiment_id="research-experiment",
        result_hash="result-hash",
        validation_policy_id="validation-policy",
        validation_policy_version="1",
        oos_status=EvidenceOosStatus.PRISTINE,
        validation_verdict=ResearchDecision.PASS,
        warnings=(),
        unavailable_dimensions=(),
        empirical_eligible=not fixture,
        deterministic_test_fixture=fixture,
    )


def approval(artifact_hash: str) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id="approval-1",
        artifact_hash=artifact_hash,
        evidence_id="evidence-1",
        target_stage=StrategyStage.SHADOW,
        actor_id="reviewer",
        policy_id="policy",
        policy_version="1",
        reason="reviewed exact immutable evidence",
        approved_at=BASE,
    )


def state(artifact_hash: str, stage: StrategyStage = StrategyStage.VALIDATION) -> StrategyState:
    return StrategyState(
        strategy_id="fixture_strategy",
        strategy_version="1",
        artifact_hash=artifact_hash,
        stage=stage,
        health=StrategyHealth.UNKNOWN,
        revision=2,
        disabled=False,
        disable_reason=None,
        updated_at=BASE,
        last_event_id=None,
    )


def test_canonical_hash_is_stable_and_parameter_types_are_distinct() -> None:
    first = artifact()
    second = artifact()
    assert first.artifact_hash == second.artifact_hash
    assert first.canonical() == second.canonical()
    changed = artifact().canonical()
    parameters = changed["parameters"]
    assert isinstance(parameters, list)
    parameters[0] = {"name": "lookback", "value": {"type": "float", "value": 3.0}}
    restored = StrategyArtifact.from_canonical(changed)
    assert restored.artifact_hash != first.artifact_hash
    assert restored.parameters[0].value == 3.0
    assert restored.parameters[0].canonical()["value"] != first.parameters[0].canonical()["value"]


def test_feature_identity_includes_parameterization() -> None:
    candidate = replace(
        artifact(),
        features=(
            FeatureDefinition("ema", "1", (StrategyParameter("window", 10),)),
            FeatureDefinition("ema", "1", (StrategyParameter("window", 30),)),
        ),
    )

    assert candidate.features[0].name == candidate.features[1].name
    assert candidate.artifact_hash != artifact().artifact_hash


@pytest.mark.parametrize("value", [float("nan"), float("inf"), Decimal("-Infinity")])
def test_nonfinite_parameters_are_rejected(value: object) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        StrategyParameter("unsafe", value)


def test_unknown_artifact_fields_and_tampered_hash_fail_closed() -> None:
    payload = artifact().canonical()
    payload["unexpected"] = True
    with pytest.raises(ValueError, match="unknown or missing"):
        StrategyArtifact.from_canonical(payload)
    with pytest.raises(ValueError, match="hash mismatch"):
        StrategyArtifact.from_canonical(artifact().canonical(), expected_hash="tampered")


def test_transition_evaluator_is_explicit_about_evidence_approval_and_live_stages() -> None:
    candidate = artifact()
    actor = TrustedActor("operator", frozenset({ActorCapability.TRANSITION}))
    policy = LifecyclePolicy("policy", "1")
    current = state(candidate.artifact_hash)
    missing = evaluate_transition(
        state=current,
        artifact=candidate,
        target_stage=StrategyStage.SHADOW,
        actor=actor,
        policy=policy,
        reason="promote",
    )
    assert missing.decision is EvaluationDecision.HOLD
    assert missing.reason_codes == ("MISSING_EVIDENCE",)

    fixture = evaluate_transition(
        state=current,
        artifact=candidate,
        target_stage=StrategyStage.SHADOW,
        actor=actor,
        policy=policy,
        evidence=evidence(candidate.artifact_hash, fixture=True),
        approval=approval(candidate.artifact_hash),
        reason="promote",
    )
    assert fixture.decision is EvaluationDecision.REJECT
    assert "FIXTURE" in fixture.reason_codes[0]

    allowed = evaluate_transition(
        state=current,
        artifact=candidate,
        target_stage=StrategyStage.SHADOW,
        actor=actor,
        policy=policy,
        evidence=evidence(candidate.artifact_hash),
        approval=approval(candidate.artifact_hash),
        reason="promote",
    )
    assert allowed.decision is EvaluationDecision.ALLOW

    live = evaluate_transition(
        state=current,
        artifact=candidate,
        target_stage=StrategyStage.ACTIVE,
        actor=actor,
        policy=policy,
        reason="live",
    )
    assert live.decision is EvaluationDecision.REJECT
    assert live.reason_codes == ("LIVE_CAPABILITY_UNAVAILABLE",)


def test_explicit_failed_strategy_health_rejects_operational_eligibility() -> None:
    candidate = artifact()
    current = replace(
        state(candidate.artifact_hash, stage=StrategyStage.SHADOW),
        health=StrategyHealth.FAILED,
    )

    result = evaluate_current_eligibility(
        state=current,
        artifact=candidate,
        evidence=evidence(candidate.artifact_hash),
        approval=approval(candidate.artifact_hash),
    )

    assert result.decision is EvaluationDecision.REJECT
    assert result.reason_codes == ("STRATEGY_HEALTH_FAILED",)


def test_evidence_and_approval_are_bound_to_exact_artifact() -> None:
    candidate = artifact()
    other = artifact(version="2")
    result = evaluate_transition(
        state=state(candidate.artifact_hash),
        artifact=other,
        target_stage=StrategyStage.SHADOW,
        actor=TrustedActor("operator", frozenset({ActorCapability.TRANSITION})),
        policy=LifecyclePolicy("policy", "1"),
        evidence=evidence(candidate.artifact_hash),
        approval=approval(candidate.artifact_hash),
        reason="mismatch",
    )
    assert result.decision is EvaluationDecision.REJECT
    assert result.reason_codes == ("EVIDENCE_ARTIFACT_MISMATCH",)
