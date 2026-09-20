"""SQLite integration coverage for the governed strategy application boundary."""

import json
import socket
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text, update

from traderos.data.bars import MarketBar
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.lineage import AdjustmentPolicy
from traderos.data.timeframes import Timeframe
from traderos.database.connection import create_database_engine
from traderos.database.schema import strategy_artifacts, strategy_lifecycle_state
from traderos.research.models import (
    DatasetManifest,
    ExperimentResult,
    ExperimentSpec,
    ResearchDecision,
    ResearchScope,
    TemporalRange,
)
from traderos.research.provenance import (
    EvidenceClassification,
    IntegrityStatus,
    ProvenanceAuthorizationError,
    ProvenanceQueryService,
)
from traderos.research.registry import ExperimentRegistry, RegisteredExperiment
from traderos.research.strategy_lifecycle import (
    ActorCapability,
    ActorDirectory,
    ApprovalRecord,
    EvidenceAttachment,
    EvidenceOosStatus,
    FeatureDefinition,
    LifecycleError,
    RegisteredImplementation,
    StrategyArtifact,
    StrategyParameter,
    StrategyStage,
)
from traderos.research.strategy_service import StrategyGovernanceService
from traderos.research.strategy_store import StrategyGovernanceStore

BASE = datetime(2024, 1, 2, tzinfo=UTC)


def _bars() -> list[MarketBar]:
    instrument = Instrument(
        canonical_symbol="EUR/USD",
        asset_class=AssetClass.FOREX,
        base_currency="EUR",
        quote_currency="USD",
        trading_currency="USD",
        provider_symbols={"fixture": "EURUSD"},
    )
    return [
        MarketBar(
            instrument=instrument,
            timeframe=Timeframe.H1,
            adjustment_policy=AdjustmentPolicy.RAW,
            timestamp=BASE + timedelta(hours=index),
            open=Decimal("1.10"),
            high=Decimal("1.11"),
            low=Decimal("1.09"),
            close=Decimal("1.10"),
            source="fixture",
            ingestion_timestamp=BASE,
        )
        for index in range(2)
    ]


def _fixture(
    tmp_path: Path, *, version: str = "1"
) -> tuple[StrategyArtifact, EvidenceAttachment, ExperimentRegistry]:
    bars = _bars()
    manifest = DatasetManifest.from_bars(
        dataset_version="governance-fixture-v1",
        bars=bars,
        quality_status="fixture",
        historical_membership_available=True,
        delistings_available=True,
        corporate_actions_verified=True,
    )
    period = TemporalRange(BASE, BASE + timedelta(hours=2))
    development_spec = ExperimentSpec.from_parameters(
        dataset=manifest,
        split_id="governance-split",
        scope=ResearchScope.DEVELOPMENT,
        period=period,
        strategy_id="fixture_strategy",
        strategy_version=version,
        parameters={"lookback": 3},
        feature_versions=(),
        regime_configuration_id=None,
        fusion_configuration_id=None,
        risk_policy_configuration_id="risk-v1",
        backtest_experiment_id=f"backtest-{version}",
        cost_scenario_id="base",
        code_version="test-code",
        random_seed=7,
        selection_eligible=True,
    )
    development_result = ExperimentResult(
        experiment_id=development_spec.experiment_id,
        metrics=(("software_fixture", "1"),),
        decision=ResearchDecision.CONDITIONAL,
        locked_oos_touched=False,
        selection_influenced_by_locked_oos=False,
    )
    spec = replace(
        development_spec,
        scope=ResearchScope.LOCKED_OUT_OF_SAMPLE,
        frozen_from_experiment_id=development_spec.experiment_id,
        selection_eligible=False,
    )
    result = ExperimentResult(
        experiment_id=spec.experiment_id,
        metrics=(("software_fixture_oos", "1"),),
        decision=ResearchDecision.PASS,
        locked_oos_touched=True,
        selection_influenced_by_locked_oos=False,
    )
    registry = ExperimentRegistry(tmp_path / "experiments")
    registry.record(RegisteredExperiment(development_spec, development_result))
    registry.record(RegisteredExperiment(spec, result))
    artifact = StrategyArtifact(
        strategy_id="fixture_strategy",
        strategy_version=version,
        family_id="fixture-family",
        author="researcher",
        implementation=RegisteredImplementation("always_flat", "1"),
        instruments=("EUR/USD",),
        timeframes=("1h",),
        parameters=(StrategyParameter("lookback", 3),),
        features=(FeatureDefinition("return", "1"),),
        training_reference=None,
        validation_reference=None,
        oos_reference=None,
        forecast_semantics="direction-only test signal",
        holding_period=None,
        turnover_assumption=None,
        cost_model_reference=None,
        capacity_model_reference=None,
        regime_dependencies=(),
        risk_dependencies=("risk-v1",),
        known_failure_modes=("fixture only",),
        code_identity="git:test",
        dataset_identity=manifest.dataset_hash,
        configuration_identity=None,
        environment_reference="pytest",
        unavailable_evidence=("empirical_oos",),
    )
    evidence = EvidenceAttachment(
        evidence_id=f"evidence-{version}",
        artifact_hash=artifact.artifact_hash,
        dataset_hashes=(manifest.dataset_hash,),
        experiment_id=spec.experiment_id,
        result_hash=result.result_hash,
        validation_policy_id="test-policy",
        validation_policy_version="1",
        oos_status=EvidenceOosStatus.PRISTINE,
        validation_verdict=ResearchDecision.PASS,
        warnings=("software fixture",),
        unavailable_dimensions=("empirical performance",),
        empirical_eligible=True,
        deterministic_test_fixture=False,
    )
    return artifact, evidence, registry


def _service(tmp_path: Path, registry: ExperimentRegistry) -> StrategyGovernanceService:
    store = StrategyGovernanceStore(
        create_database_engine(f"sqlite:///{tmp_path / 'governance.db'}")
    )
    store.create_schema_for_testing()
    actors = ActorDirectory(
        {
            "researcher": (ActorCapability.RESEARCH, ActorCapability.APPROVE),
            "operator": (ActorCapability.TRANSITION, ActorCapability.DISABLE),
            "reviewer": (ActorCapability.APPROVE,),
            "retirement": (ActorCapability.RETIRE,),
        }
    )
    return StrategyGovernanceService(store, actors, experiment_registry=registry)


def test_governed_progression_is_durable_idempotent_and_side_effect_free(tmp_path: Path) -> None:
    artifact, evidence, registry = _fixture(tmp_path)
    service = _service(tmp_path, registry)
    service.register_strategy_artifact(artifact, actor_id="researcher")
    assert service.register_strategy_artifact(artifact, actor_id="researcher") == artifact
    with pytest.raises(LifecycleError, match="conflicting reuse"):
        service.register_strategy_artifact(
            artifact.__class__(
                **{**artifact.__dict__, "strategy_version": "1", "forecast_semantics": "changed"}
            ),
            actor_id="researcher",
        )

    state = service.get_strategy_state("fixture_strategy", "1")
    assert state.stage is StrategyStage.CANDIDATE
    service.transition_strategy(
        "fixture_strategy",
        "1",
        target_stage=StrategyStage.RESEARCH,
        actor_id="researcher",
        expected_revision=0,
        idempotency_key="research-1",
        reason="research opened",
    )
    service.attach_evidence(evidence, actor_id="researcher")
    service.transition_strategy(
        "fixture_strategy",
        "1",
        target_stage=StrategyStage.VALIDATION,
        actor_id="researcher",
        expected_revision=1,
        idempotency_key="validation-1",
        evidence_id=evidence.evidence_id,
        reason="validation recorded",
    )
    approval = ApprovalRecord(
        approval_id="approval-1",
        artifact_hash=artifact.artifact_hash,
        evidence_id=evidence.evidence_id,
        target_stage=StrategyStage.SHADOW,
        actor_id="reviewer",
        policy_id="test-policy",
        policy_version="1",
        reason="independent review",
        approved_at=BASE,
    )
    service.request_or_record_approval(approval=approval, actor_id="reviewer")
    service.transition_strategy(
        "fixture_strategy",
        "1",
        target_stage=StrategyStage.SHADOW,
        actor_id="operator",
        expected_revision=2,
        idempotency_key="shadow-1",
        evidence_id=evidence.evidence_id,
        approval_id=approval.approval_id,
        reason="approved shadow entry",
    )
    state = service.get_strategy_state("fixture_strategy", "1")
    assert state.stage is StrategyStage.SHADOW
    assert state.revision == 3
    assert (
        service.store.list_history("fixture_strategy", "1")[0].resulting_stage
        is StrategyStage.RESEARCH
    )

    # A lost acknowledgement can be retried with the original identity.
    retried = service.transition_strategy(
        "fixture_strategy",
        "1",
        target_stage=StrategyStage.SHADOW,
        actor_id="operator",
        expected_revision=2,
        idempotency_key="shadow-1",
        evidence_id=evidence.evidence_id,
        approval_id=approval.approval_id,
        reason="approved shadow entry",
    )
    assert retried.revision == 3
    with pytest.raises(LifecycleError, match="idempotency key"):
        service.transition_strategy(
            "fixture_strategy",
            "1",
            target_stage=StrategyStage.SHADOW,
            actor_id="operator",
            expected_revision=2,
            idempotency_key="shadow-1",
            evidence_id=evidence.evidence_id,
            approval_id=approval.approval_id,
            reason="different request",
        )

    assert service.reconcile_strategy("fixture_strategy", "1").revision == 3
    restarted = _service(tmp_path, registry)
    assert restarted.get_strategy_state("fixture_strategy", "1").revision == 3
    with service.store.engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM paper_orders")).scalar_one() == 0
        assert connection.execute(text("SELECT COUNT(*) FROM paper_fills")).scalar_one() == 0
        assert connection.execute(text("SELECT COUNT(*) FROM paper_accounts")).scalar_one() == 0
    with service.store.engine.begin() as connection:
        connection.execute(
            update(strategy_lifecycle_state)
            .where(
                strategy_lifecycle_state.c.strategy_id == "fixture_strategy",
                strategy_lifecycle_state.c.strategy_version == "1",
            )
            .values(revision=99)
        )
    with pytest.raises(LifecycleError, match="projection"):
        restarted.reconcile_strategy("fixture_strategy", "1")


def test_equal_timestamp_evidence_and_approval_order_is_deterministic(tmp_path: Path) -> None:
    artifact, evidence, registry = _fixture(tmp_path)
    service = _service(tmp_path, registry)
    service.register_strategy_artifact(artifact, actor_id="researcher")

    older_identity = replace(evidence, evidence_id="evidence-a")
    newer_identity = replace(evidence, evidence_id="evidence-z")
    service.store.attach_evidence(older_identity, attached_at=BASE)
    service.store.attach_evidence(newer_identity, attached_at=BASE)

    explanation = service.explain_strategy_eligibility("fixture_strategy", "1")
    assert explanation.evidence_id == "evidence-z"
    assert tuple(
        item.evidence_id for item in service.store.list_evidence(artifact.artifact_hash)
    ) == ("evidence-z", "evidence-a")

    for approval_id in ("approval-a", "approval-z"):
        service.store.record_approval(
            ApprovalRecord(
                approval_id=approval_id,
                artifact_hash=artifact.artifact_hash,
                evidence_id=newer_identity.evidence_id,
                target_stage=StrategyStage.SHADOW,
                actor_id="reviewer",
                policy_id="test-policy",
                policy_version="1",
                reason="same-time review",
                approved_at=BASE,
            )
        )
    assert tuple(
        item.approval_id
        for item in service.store.list_approvals(
            artifact.artifact_hash, newer_identity.evidence_id
        )
    ) == ("approval-z", "approval-a")


def test_disable_is_version_scoped_and_revocation_blocks_future_eligibility(tmp_path: Path) -> None:
    artifact, evidence, registry = _fixture(tmp_path)
    service = _service(tmp_path, registry)
    service.register_strategy_artifact(artifact, actor_id="researcher")
    service.attach_evidence(evidence, actor_id="researcher")
    approval = ApprovalRecord(
        approval_id="approval-disable",
        artifact_hash=artifact.artifact_hash,
        evidence_id=evidence.evidence_id,
        target_stage=StrategyStage.SHADOW,
        actor_id="reviewer",
        policy_id="test-policy",
        policy_version="1",
        reason="review",
        approved_at=BASE,
    )
    service.request_or_record_approval(approval=approval, actor_id="reviewer")
    service.disable_strategy(
        "fixture_strategy",
        "1",
        actor_id="operator",
        expected_revision=0,
        idempotency_key="disable-1",
        reason="data quality review",
    )
    explanation = service.explain_strategy_eligibility("fixture_strategy", "1")
    assert explanation.disabled
    assert "STRATEGY_DISABLED" in explanation.reasons
    with pytest.raises(LifecycleError, match="research roles"):
        service.request_or_record_approval(
            approval=ApprovalRecord(
                approval_id="approval-self",
                artifact_hash=artifact.artifact_hash,
                evidence_id=evidence.evidence_id,
                target_stage=StrategyStage.SHADOW,
                actor_id="researcher",
                policy_id="test-policy",
                policy_version="1",
                reason="self",
                approved_at=BASE,
            ),
            actor_id="researcher",
        )
    service.store.revoke_evidence(
        evidence_id=evidence.evidence_id,
        actor_id="reviewer",
        reason="evidence withdrawn",
        idempotency_key="revoke-evidence-1",
        revoked_at=BASE,
    )
    revoked_explanation = service.explain_strategy_eligibility("fixture_strategy", "1")
    assert "EVIDENCE_REVOKED" in revoked_explanation.reasons

    version_two, evidence_two, _ = _fixture(tmp_path, version="2")
    service.register_strategy_artifact(version_two, actor_id="researcher")
    service.attach_evidence(evidence_two, actor_id="researcher")
    assert service.get_strategy_state("fixture_strategy", "2").revision == 0


def test_tampered_reads_and_database_rollback_fail_closed(tmp_path: Path) -> None:
    artifact, _, registry = _fixture(tmp_path)
    service = _service(tmp_path, registry)
    service.register_strategy_artifact(artifact, actor_id="researcher")
    with service.store.engine.begin() as connection:
        connection.execute(
            update(strategy_artifacts)
            .where(strategy_artifacts.c.artifact_hash == artifact.artifact_hash)
            .values(canonical_payload={**artifact.canonical(), "forecast_semantics": "tampered"})
        )
    with pytest.raises(LifecycleError, match="hash mismatch"):
        service.get_strategy_artifact("fixture_strategy", "1")

    # Restore a clean database for the transactional failure check.
    artifact, _, registry = _fixture(tmp_path, version="3")
    service = _service(tmp_path, registry)
    service.register_strategy_artifact(artifact, actor_id="researcher")
    with service.store.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER fail_strategy_event BEFORE INSERT ON strategy_lifecycle_events "
                "BEGIN SELECT RAISE(ABORT, 'test rollback'); END"
            )
        )
    with pytest.raises(LifecycleError, match="transaction failed"):
        service.transition_strategy(
            "fixture_strategy",
            "3",
            target_stage=StrategyStage.RESEARCH,
            actor_id="researcher",
            expected_revision=0,
            idempotency_key="rollback-1",
            reason="must roll back",
        )
    assert service.get_strategy_state("fixture_strategy", "3").revision == 0
    assert service.store.list_history("fixture_strategy", "3") == ()


def _enter_shadow(
    service: StrategyGovernanceService,
    artifact: StrategyArtifact,
    evidence: EvidenceAttachment,
) -> ApprovalRecord:
    service.register_strategy_artifact(artifact, actor_id="researcher")
    service.attach_evidence(evidence, actor_id="researcher")
    service.transition_strategy(
        artifact.strategy_id,
        artifact.strategy_version,
        target_stage=StrategyStage.RESEARCH,
        actor_id="researcher",
        expected_revision=0,
        idempotency_key=f"research-{artifact.strategy_version}",
        reason="open research",
    )
    service.transition_strategy(
        artifact.strategy_id,
        artifact.strategy_version,
        target_stage=StrategyStage.VALIDATION,
        actor_id="researcher",
        expected_revision=1,
        idempotency_key=f"validation-{artifact.strategy_version}",
        evidence_id=evidence.evidence_id,
        reason="record validation",
    )
    approval = ApprovalRecord(
        approval_id=f"approval-{artifact.strategy_version}",
        artifact_hash=artifact.artifact_hash,
        evidence_id=evidence.evidence_id,
        target_stage=StrategyStage.SHADOW,
        actor_id="reviewer",
        policy_id="test-policy",
        policy_version="1",
        reason="independent review",
        approved_at=BASE,
    )
    service.request_or_record_approval(approval=approval, actor_id="reviewer")
    service.transition_strategy(
        artifact.strategy_id,
        artifact.strategy_version,
        target_stage=StrategyStage.SHADOW,
        actor_id="operator",
        expected_revision=2,
        idempotency_key=f"shadow-{artifact.strategy_version}",
        evidence_id=evidence.evidence_id,
        approval_id=approval.approval_id,
        reason="enter shadow",
    )
    return approval


def _provenance_service(
    service: StrategyGovernanceService, registry: ExperimentRegistry
) -> ProvenanceQueryService:
    return ProvenanceQueryService(
        service.store,
        registry,
        ActorDirectory(
            {
                "reader": (ActorCapability.PROVENANCE_READ,),
                "no-read": (),
            }
        ),
        clock=lambda: BASE + timedelta(days=10),
    )


def test_provenance_graph_is_integrity_checked_bounded_and_side_effect_free(tmp_path: Path) -> None:
    artifact, evidence, registry = _fixture(tmp_path)
    service = _service(tmp_path, registry)
    approval = _enter_shadow(service, artifact, evidence)
    provenance = _provenance_service(service, registry)

    graph = provenance.get_strategy_provenance(
        "fixture_strategy",
        "1",
        actor_id="reader",
        history_limit=2,
        evidence_limit=10,
        approval_limit=10,
    )
    assert [node.node_id for node in graph.nodes] == sorted(node.node_id for node in graph.nodes)
    artifact_node = next(
        node for node in graph.nodes if node.entity_type.value == "strategy_artifact"
    )
    assert artifact_node.content_hash == artifact.artifact_hash
    assert artifact_node.integrity_status is IntegrityStatus.VERIFIED
    evidence_node = next(
        node for node in graph.nodes if node.stable_identity == evidence.evidence_id
    )
    assert evidence_node.empirical_classification is EvidenceClassification.EMPIRICAL
    assert any(
        relationship.relationship_type.value == "evidence_references_experiment"
        for relationship in graph.relationships
    )
    assert any(
        finding.code == "DATASET_MANIFEST_REGISTRY_UNAVAILABLE" for finding in graph.findings
    )
    assert graph.eligibility is not None
    assert graph.eligibility.decision.value == "allow"
    assert graph.eligibility.artifact_hash == artifact.artifact_hash
    assert graph.governance_revision == 3
    assert graph.history_truncated
    assert graph.next_after_revision == 2

    page = provenance.get_strategy_provenance(
        "fixture_strategy",
        "1",
        actor_id="reader",
        history_limit=2,
        after_revision=graph.next_after_revision or 0,
    )
    first_events = {
        node.stable_identity for node in graph.nodes if node.entity_type.value == "lifecycle_event"
    }
    second_events = {
        node.stable_identity for node in page.nodes if node.entity_type.value == "lifecycle_event"
    }
    assert first_events.isdisjoint(second_events)
    assert not page.history_truncated

    first_export = provenance.export_provenance_bundle("fixture_strategy", "1", actor_id="reader")
    second_export = provenance.export_provenance_bundle("fixture_strategy", "1", actor_id="reader")
    assert first_export.content_digest == second_export.content_digest
    assert first_export.content == second_export.content
    destination = tmp_path / "exports" / "strategy.json"
    destination.parent.mkdir()
    written = provenance.export_provenance_bundle(
        "fixture_strategy", "1", actor_id="reader", destination=destination
    )
    assert destination.is_file()
    assert json.loads(destination.read_text(encoding="utf-8"))["content_digest"] == (
        written.content_digest
    )

    with service.store.engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM paper_orders")).scalar_one() == 0
        assert connection.execute(text("SELECT COUNT(*) FROM paper_fills")).scalar_one() == 0
        assert connection.execute(text("SELECT COUNT(*) FROM paper_accounts")).scalar_one() == 0
    assert approval.approval_id in {item.approval_id for item in graph.eligibility.active_approvals}


def test_provenance_resolution_does_not_use_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact, evidence, registry = _fixture(tmp_path)
    service = _service(tmp_path, registry)
    _enter_shadow(service, artifact, evidence)
    provenance = _provenance_service(service, registry)

    def fail_socket(*args: object, **kwargs: object) -> None:
        raise AssertionError("provenance resolution attempted a network call")

    monkeypatch.setattr(socket, "socket", fail_socket)
    graph = provenance.get_strategy_provenance("fixture_strategy", "1", actor_id="reader")
    assert graph.eligibility is not None


def test_provenance_authorization_tampering_and_revocation_fail_closed(tmp_path: Path) -> None:
    artifact, evidence, registry = _fixture(tmp_path)
    service = _service(tmp_path, registry)
    _enter_shadow(service, artifact, evidence)
    provenance = _provenance_service(service, registry)
    with pytest.raises(ProvenanceAuthorizationError, match="unauthorized"):
        provenance.get_strategy_provenance("fixture_strategy", "1", actor_id="no-read")
    with pytest.raises(ProvenanceAuthorizationError, match="unauthorized"):
        provenance.get_strategy_provenance("unknown", "1", actor_id="unknown")

    with service.store.engine.begin() as connection:
        connection.execute(
            update(strategy_artifacts)
            .where(strategy_artifacts.c.artifact_hash == artifact.artifact_hash)
            .values(canonical_payload={**artifact.canonical(), "forecast_semantics": "tampered"})
        )
    tampered = provenance.get_strategy_provenance("fixture_strategy", "1", actor_id="reader")
    assert any(
        node.entity_type.value == "strategy_artifact"
        and node.integrity_status is IntegrityStatus.HASH_MISMATCH
        for node in tampered.nodes
    )
    assert tampered.eligibility is not None
    assert "ARTIFACT_INTEGRITY_UNVERIFIED" in tampered.eligibility.blocking_reason_codes

    # The artifact remains corrupt; the query never repairs it.  Revocation is
    # still represented only after the authoritative store is explicitly used.
    service.store.revoke_evidence(
        evidence_id=evidence.evidence_id,
        actor_id="reviewer",
        reason="withdrawn",
        idempotency_key="provenance-revoke",
        revoked_at=BASE + timedelta(days=1),
    )
    revoked = provenance.get_strategy_provenance("fixture_strategy", "1", actor_id="reader")
    assert any(node.entity_type.value == "revocation" for node in revoked.nodes)
    assert any(
        relationship.relationship_type.value == "revocation_invalidates_evidence"
        for relationship in revoked.relationships
    )


def test_historical_event_explanation_distinguishes_later_revocation(tmp_path: Path) -> None:
    artifact, evidence, registry = _fixture(tmp_path)
    service = _service(tmp_path, registry)
    approval = _enter_shadow(service, artifact, evidence)
    event = service.store.list_history("fixture_strategy", "1")[-1]
    provenance = _provenance_service(service, registry)
    service.store.revoke_evidence(
        evidence_id=evidence.evidence_id,
        actor_id="reviewer",
        reason="later review",
        idempotency_key="historical-revoke",
        revoked_at=event.occurred_at + timedelta(seconds=1),
    )
    explanation = provenance.explain_lifecycle_event(
        "fixture_strategy",
        "1",
        event.event_id,
        actor_id="reader",
    )
    assert explanation.integrity_status is IntegrityStatus.VERIFIED
    assert explanation.evidence_id == evidence.evidence_id
    assert explanation.approval_id == approval.approval_id
    assert explanation.evidence_revoked_at_event is False
    assert explanation.evidence_revoked_now is True
    assert not explanation.historical_reconstruction_complete


def test_experiment_provenance_reports_fixture_and_missing_references(tmp_path: Path) -> None:
    artifact, evidence, registry = _fixture(tmp_path)
    service = _service(tmp_path, registry)
    service.register_strategy_artifact(artifact, actor_id="researcher")
    fixture_evidence = replace(
        evidence,
        evidence_id="fixture-only",
        empirical_eligible=False,
        deterministic_test_fixture=True,
    )
    service.attach_evidence(fixture_evidence, actor_id="researcher")
    provenance = _provenance_service(service, registry)

    graph = provenance.get_experiment_provenance(
        evidence.experiment_id,
        actor_id="reader",
    )
    experiment_node = next(node for node in graph.nodes if node.entity_type.value == "experiment")
    assert experiment_node.integrity_status is IntegrityStatus.VERIFIED
    result_node = next(
        node for node in graph.nodes if node.entity_type.value == "experiment_result"
    )
    assert result_node.integrity_status is IntegrityStatus.VERIFIED
    dataset_node = next(
        node for node in graph.nodes if node.entity_type.value == "dataset_manifest"
    )
    assert dataset_node.integrity_status is IntegrityStatus.UNSUPPORTED
    assert any(
        finding.code == "EXPERIMENT_DOES_NOT_EMBED_ARTIFACT_HASH" for finding in graph.findings
    )

    fixture_graph = provenance.get_strategy_provenance("fixture_strategy", "1", actor_id="reader")
    fixture_node = next(
        node for node in fixture_graph.nodes if node.stable_identity == "fixture-only"
    )
    assert fixture_node.empirical_classification is EvidenceClassification.TEST_FIXTURE
    assert fixture_graph.eligibility is not None
    assert fixture_graph.eligibility.decision.value != "allow"

    missing = provenance.get_experiment_provenance(
        "research-000000000000000000000000", actor_id="reader"
    )
    assert missing.nodes[0].entity_type.value == "experiment"
    assert missing.nodes[0].integrity_status is IntegrityStatus.MISSING


def test_experiment_hash_and_schema_tampering_are_not_valid_evidence(tmp_path: Path) -> None:
    artifact, evidence, registry = _fixture(tmp_path)
    service = _service(tmp_path, registry)
    service.register_strategy_artifact(artifact, actor_id="researcher")
    service.attach_evidence(evidence, actor_id="researcher")
    provenance = _provenance_service(service, registry)
    path = registry.root / f"{evidence.experiment_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["result"]["result_hash"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    graph = provenance.get_experiment_provenance(evidence.experiment_id, actor_id="reader")
    experiment_node = next(node for node in graph.nodes if node.entity_type.value == "experiment")
    assert experiment_node.integrity_status is IntegrityStatus.HASH_MISMATCH

    path.write_text(json.dumps({"schema_version": "unrelated"}), encoding="utf-8")
    invalid = provenance.get_experiment_provenance(evidence.experiment_id, actor_id="reader")
    invalid_experiment = next(
        node for node in invalid.nodes if node.entity_type.value == "experiment"
    )
    assert invalid_experiment.integrity_status is IntegrityStatus.INVALID_SCHEMA
