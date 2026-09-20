"""Integration coverage for bound dataset provenance reads."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine
from tests.unit.research.test_dataset_records import BASE, _binding, _write_dataset

from traderos.data.lineage import AdjustmentPolicy
from traderos.data.timeframes import Timeframe
from traderos.research.dataset import DatasetQualificationStatus, ResearchDatasetEligibility
from traderos.research.dataset_records import (
    DatasetQualificationRecord,
    DatasetQualificationRegistry,
)
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
    ProvenanceQueryService,
)
from traderos.research.registry import ExperimentRegistry, RegisteredExperiment
from traderos.research.strategy_lifecycle import ActorCapability, ActorDirectory
from traderos.research.strategy_store import StrategyGovernanceStore


def _bound_service(tmp_path: Path) -> tuple[ProvenanceQueryService, str, str]:
    reader, content_hash, manifest_hash = _write_dataset(tmp_path)
    qualification = DatasetQualificationRecord(
        dataset_id="dataset-fixture-1",
        dataset_content_hash=content_hash,
        manifest_hash=manifest_hash,
        policy_id="fixture-policy",
        policy_version="1",
        implementation_id="fixture-qualification",
        implementation_version="1",
        verdict=DatasetQualificationStatus.QUALIFIED,
        empirical_eligibility=ResearchDatasetEligibility.DETERMINISTIC_TEST_FIXTURE,
        checked_at=BASE,
        check_results=(("normalized", "passed"),),
        warnings=("synthetic fixture",),
        unavailable_checks=("market authenticity",),
        report_reference=None,
        fixture=True,
    )
    qualification_registry = DatasetQualificationRegistry(tmp_path / "qualifications")
    qualification_registry.record(qualification)
    binding = replace(
        _binding("dataset-fixture-1", content_hash, manifest_hash),
        qualification_id=qualification.qualification_id,
    )
    period = TemporalRange(BASE + timedelta(hours=1), BASE + timedelta(hours=3))
    manifest = DatasetManifest(
        dataset_version="dataset-fixture-1",
        dataset_hash=content_hash,
        symbols=("EUR/USD",),
        timeframe=Timeframe.H1,
        period=period,
        adjustment_policy=AdjustmentPolicy.RAW,
        source="fixture",
        quality_status="fixture",
        historical_membership_available=True,
        delistings_available=True,
        corporate_actions_verified=True,
    )
    spec = ExperimentSpec.from_parameters(
        dataset=manifest,
        split_id="fixture-split",
        scope=ResearchScope.DEVELOPMENT,
        period=period,
        strategy_id="fixture-bound",
        strategy_version="1",
        parameters={"lookback": 2},
        feature_versions=(),
        regime_configuration_id=None,
        fusion_configuration_id=None,
        risk_policy_configuration_id="risk-fixture-v1",
        backtest_experiment_id="backtest-fixture-bound",
        cost_scenario_id="base",
        code_version="test-fixture",
        random_seed=1,
        selection_eligible=False,
        input_binding=binding,
    )
    result = ExperimentResult(
        experiment_id=spec.experiment_id,
        metrics=(("software_fixture", "1"),),
        decision=ResearchDecision.PASS,
        locked_oos_touched=False,
        selection_influenced_by_locked_oos=False,
    )
    experiment_registry = ExperimentRegistry(tmp_path / "experiments")
    experiment_registry.record(RegisteredExperiment(spec, result))
    store = StrategyGovernanceStore(create_engine(f"sqlite:///{tmp_path / 'governance.db'}"))
    store.create_schema_for_testing()
    service = ProvenanceQueryService(
        store,
        experiment_registry,
        ActorDirectory({"reader": (ActorCapability.PROVENANCE_READ,)}),
        dataset_reader=reader,
        qualification_registry=qualification_registry,
    )
    return service, spec.experiment_id, qualification.qualification_id


def test_bound_experiment_provenance_exposes_verified_edges_and_fixture_status(
    tmp_path: Path,
) -> None:
    service, experiment_id, qualification_id = _bound_service(tmp_path)
    graph = service.get_experiment_provenance(experiment_id, actor_id="reader")
    assert any(
        node.entity_type.value == "dataset" and node.integrity_status is IntegrityStatus.VERIFIED
        for node in graph.nodes
    )
    assert any(
        node.entity_type.value == "dataset_qualification"
        and node.stable_identity == qualification_id
        and node.empirical_classification is EvidenceClassification.TEST_FIXTURE
        for node in graph.nodes
    )
    relationship_types = {item.relationship_type.value for item in graph.relationships}
    assert "experiment_uses_qualification" in relationship_types
    assert "data_slice_derived_from_dataset" in relationship_types
    assert not any(item.code == "EXPERIMENT_INPUT_BINDING_MISSING" for item in graph.findings)

    explanation = service.explain_experiment_data_eligibility(
        experiment_id, actor_id="reader"
    )
    assert explanation.binding_status == "BOUND_VERIFIED"
    assert explanation.empirical_eligibility == "deterministic_test_fixture"
    assert explanation.oos_status == "PRISTINE"


def test_legacy_experiment_is_explicitly_unbound(tmp_path: Path) -> None:
    reader, _, _ = _write_dataset(tmp_path)
    del reader
    # The existing fixture governance path is intentionally v1 and has no
    # binding.  This test uses the smallest registry document to ensure the
    # read model reports that fact instead of inferring a dataset link.
    from tests.integration.research.test_strategy_governance import _fixture

    _, _, registry = _fixture(tmp_path / "legacy")
    store = StrategyGovernanceStore(create_engine(f"sqlite:///{tmp_path / 'legacy.db'}"))
    store.create_schema_for_testing()
    service = ProvenanceQueryService(
        store,
        registry,
        ActorDirectory({"reader": (ActorCapability.PROVENANCE_READ,)}),
    )
    experiment_id = next(path.stem for path in registry.root.glob("research-*.json"))
    explanation = service.explain_experiment_data_eligibility(
        experiment_id, actor_id="reader"
    )
    assert explanation.binding_status == "LEGACY_UNBOUND"
    assert "EXPERIMENT_INPUT_BINDING_MISSING" in explanation.reasons
