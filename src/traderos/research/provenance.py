"""Read-only research and governance provenance.

The provenance layer is an integrity-verifying read model.  It does not write
governance state, approve transitions, submit orders, or call external
services.  SQL state is read in one database snapshot; immutable experiment
records are read locally through :class:`ExperimentRegistry` and are therefore
explicitly reported as a separate consistency boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import Engine, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from traderos.data.time import require_utc
from traderos.database.schema import (
    strategy_approval_revocations,
    strategy_approvals,
    strategy_artifacts,
    strategy_evidence,
    strategy_evidence_revocations,
    strategy_lifecycle_events,
    strategy_lifecycle_state,
)
from traderos.research.dataset import DatasetQualificationStatus, ResearchDatasetEligibility
from traderos.research.dataset_records import (
    DatasetQualificationRecord,
    DatasetQualificationRegistry,
    DatasetRecordRead,
    DatasetRecordReader,
    DatasetRecordStatus,
    QualificationRead,
)
from traderos.research.models import (
    ExperimentInputBinding,
    ExperimentResult,
    ResearchDecision,
    ResearchError,
    ResearchScope,
    TemporalRange,
)
from traderos.research.registry import ExperimentRegistry
from traderos.research.strategy_lifecycle import (
    ActorCapability,
    ActorDirectory,
    ApprovalRecord,
    EvaluationDecision,
    EvidenceAttachment,
    LifecycleError,
    LifecycleEvent,
    RevocationRecord,
    StrategyArtifact,
    StrategyHealth,
    StrategyStage,
    StrategyState,
    evaluate_current_eligibility,
)
from traderos.research.strategy_store import StrategyGovernanceStore


class ProvenanceError(ResearchError):
    """Raised for a malformed or unavailable provenance query."""


class ProvenanceAuthorizationError(ProvenanceError):
    """Raised without disclosing whether the requested strategy exists."""


class IntegrityStatus(StrEnum):
    VERIFIED = "verified"
    MISSING = "missing"
    INVALID_SCHEMA = "invalid_schema"
    HASH_MISMATCH = "hash_mismatch"
    UNAUTHORIZED = "unauthorized"
    UNSUPPORTED = "unsupported"


class ProvenanceNodeType(StrEnum):
    DATASET = "dataset"
    DATASET_MANIFEST = "dataset_manifest"
    DATASET_ADMISSION = "dataset_admission"
    DATASET_QUALIFICATION = "dataset_qualification"
    RAW_ARTIFACT = "raw_artifact"
    DATA_SLICE = "data_slice"
    HYPOTHESIS = "hypothesis"
    CANDIDATE = "candidate"
    EXPERIMENT = "experiment"
    EXPERIMENT_RESULT = "experiment_result"
    STRATEGY_ARTIFACT = "strategy_artifact"
    EVIDENCE = "evidence"
    APPROVAL = "approval"
    REVOCATION = "revocation"
    LIFECYCLE_EVENT = "lifecycle_event"
    STRATEGY_STATE = "strategy_state"
    POLICY = "policy"


class ProvenanceRelationshipType(StrEnum):
    RAW_ARTIFACT_SUPPORTS_DATASET = "raw_artifact_supports_dataset"
    MANIFEST_DESCRIBES_DATASET = "manifest_describes_dataset"
    ADMISSION_EVALUATES_DATASET = "admission_evaluates_dataset"
    QUALIFICATION_EVALUATES_DATASET = "qualification_evaluates_dataset"
    EXPERIMENT_USES_DATASET = "experiment_uses_dataset"
    EXPERIMENT_USES_QUALIFICATION = "experiment_uses_qualification"
    DATA_SLICE_DERIVED_FROM_DATASET = "data_slice_derived_from_dataset"
    EXPERIMENT_EVALUATES_ARTIFACT = "experiment_evaluates_artifact"
    RESULT_BELONGS_TO_EXPERIMENT = "result_belongs_to_experiment"
    EVIDENCE_SUPPORTS_ARTIFACT = "evidence_supports_artifact"
    EVIDENCE_REFERENCES_EXPERIMENT = "evidence_references_experiment"
    EVIDENCE_REFERENCES_DATASET = "evidence_references_dataset"
    APPROVAL_AUTHORIZES_TRANSITION = "approval_authorizes_transition"
    APPROVAL_BINDS_EVIDENCE = "approval_binds_evidence"
    EVENT_APPLIES_TO_ARTIFACT = "event_applies_to_artifact"
    EVENT_REFERENCES_EVIDENCE = "event_references_evidence"
    EVENT_REFERENCES_APPROVAL = "event_references_approval"
    REVOCATION_INVALIDATES_EVIDENCE = "revocation_invalidates_evidence"
    REVOCATION_INVALIDATES_APPROVAL = "revocation_invalidates_approval"
    APPLIES_POLICY = "applies_policy"
    ARTIFACT_REFERENCES_EXPERIMENT = "artifact_references_experiment"
    EXPERIMENT_REFERENCES_CANDIDATE = "experiment_references_candidate"
    STATE_PROJECTS_ARTIFACT = "state_projects_artifact"


class EvidenceClassification(StrEnum):
    EMPIRICAL = "empirical"
    TEST_FIXTURE = "test_fixture"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _canonical_bytes(payload: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProvenanceError("provenance contains a non-canonical value") from exc


def _node_id(entity_type: ProvenanceNodeType, stable_identity: str) -> str:
    return f"{entity_type.value}:{stable_identity}"


def _classify_registry_failure(error: ResearchError) -> IntegrityStatus:
    message = str(error).lower()
    if "does not exist" in message:
        return IntegrityStatus.MISSING
    if "hash mismatch" in message:
        return IntegrityStatus.HASH_MISMATCH
    return IntegrityStatus.INVALID_SCHEMA


@dataclass(frozen=True)
class ProvenanceTimestamp:
    name: str
    value: datetime
    semantics: str

    def __post_init__(self) -> None:
        require_utc(self.value)
        if not self.name.strip() or not self.semantics.strip():
            raise ProvenanceError("provenance timestamp metadata must be non-blank")

    def canonical(self) -> dict[str, str]:
        return {
            "name": self.name,
            "value": self.value.isoformat(),
            "semantics": self.semantics,
        }


@dataclass(frozen=True)
class ProvenanceNode:
    entity_type: ProvenanceNodeType
    schema_version: str
    stable_identity: str
    content_hash: str | None
    timestamps: tuple[ProvenanceTimestamp, ...]
    integrity_status: IntegrityStatus
    source_reference: str
    empirical_classification: EvidenceClassification
    attributes: tuple[tuple[str, object], ...] = ()

    def __post_init__(self) -> None:
        if not self.schema_version.strip() or not self.stable_identity.strip():
            raise ProvenanceError("provenance node schema and identity are required")
        if not self.source_reference.strip():
            raise ProvenanceError("provenance node source reference is required")
        if self.content_hash is not None and not self.content_hash.strip():
            raise ProvenanceError("provenance node content hash must be non-blank")
        names = [name for name, _ in self.attributes]
        if len(names) != len(set(names)) or any(not name.strip() for name in names):
            raise ProvenanceError("provenance node attribute names must be unique")

    @property
    def node_id(self) -> str:
        return _node_id(self.entity_type, self.stable_identity)

    def canonical(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "entity_type": self.entity_type.value,
            "schema_version": self.schema_version,
            "stable_identity": self.stable_identity,
            "content_hash": self.content_hash,
            "timestamps": [item.canonical() for item in self.timestamps],
            "integrity_status": self.integrity_status.value,
            "source_reference": self.source_reference,
            "empirical_classification": self.empirical_classification.value,
            "attributes": {name: value for name, value in sorted(self.attributes)},
        }


@dataclass(frozen=True)
class ProvenanceRelationship:
    relationship_type: ProvenanceRelationshipType
    source_node_id: str
    target_node_id: str
    integrity_status: IntegrityStatus
    source_reference: str
    evidence_references: tuple[str, ...] = ()
    explanation: str = ""

    @property
    def relationship_id(self) -> str:
        payload = {
            "relationship_type": self.relationship_type.value,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "integrity_status": self.integrity_status.value,
            "source_reference": self.source_reference,
            "evidence_references": self.evidence_references,
            "explanation": self.explanation,
        }
        return "relationship-" + hashlib.sha256(_canonical_bytes(payload)).hexdigest()[:32]

    def canonical(self) -> dict[str, object]:
        return {
            "relationship_id": self.relationship_id,
            "relationship_type": self.relationship_type.value,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "integrity_status": self.integrity_status.value,
            "source_reference": self.source_reference,
            "evidence_references": list(self.evidence_references),
            "explanation": self.explanation,
        }


@dataclass(frozen=True)
class ProvenanceFinding:
    code: str
    integrity_status: IntegrityStatus
    entity_type: ProvenanceNodeType
    stable_identity: str

    def canonical(self) -> dict[str, str]:
        return {
            "code": self.code,
            "integrity_status": self.integrity_status.value,
            "entity_type": self.entity_type.value,
            "stable_identity": self.stable_identity,
        }


@dataclass(frozen=True)
class EvidenceEligibilitySummary:
    evidence_id: str
    integrity_status: IntegrityStatus
    empirical_eligible: bool | None
    deterministic_test_fixture: bool | None
    oos_status: str | None
    validation_verdict: str | None
    revoked: bool

    def canonical(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "integrity_status": self.integrity_status.value,
            "empirical_eligible": self.empirical_eligible,
            "deterministic_test_fixture": self.deterministic_test_fixture,
            "oos_status": self.oos_status,
            "validation_verdict": self.validation_verdict,
            "revoked": self.revoked,
        }


@dataclass(frozen=True)
class ApprovalEligibilitySummary:
    approval_id: str
    target_stage: str | None
    integrity_status: IntegrityStatus
    revoked: bool

    def canonical(self) -> dict[str, object]:
        return {
            "approval_id": self.approval_id,
            "target_stage": self.target_stage,
            "integrity_status": self.integrity_status.value,
            "revoked": self.revoked,
        }


@dataclass(frozen=True)
class ProvenanceEligibilityExplanation:
    strategy_id: str
    strategy_version: str
    artifact_hash: str | None
    stage: StrategyStage | None
    revision: int | None
    disabled: bool | None
    health: StrategyHealth | None
    decision: EvaluationDecision
    active_approvals: tuple[ApprovalEligibilitySummary, ...]
    revoked_approvals: tuple[ApprovalEligibilitySummary, ...]
    evidence: tuple[EvidenceEligibilitySummary, ...]
    policy_references: tuple[tuple[str, str], ...]
    missing_prerequisites: tuple[str, ...]
    blocking_reason_codes: tuple[str, ...]
    review_requirements: tuple[str, ...]
    summary: str
    query_timestamp: datetime
    consistency_boundary: str
    data_binding_status: str = "unknown"
    dataset_status: IntegrityStatus | None = None
    dataset_content_hash: str | None = None
    dataset_manifest_hash: str | None = None
    qualification_id: str | None = None
    qualification_status: IntegrityStatus | None = None
    qualification_verdict: str | None = None
    empirical_eligibility: str | None = None
    oos_data_status: str | None = None

    def __post_init__(self) -> None:
        require_utc(self.query_timestamp)

    def canonical(self) -> dict[str, object]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "artifact_hash": self.artifact_hash,
            "stage": self.stage.value if self.stage is not None else None,
            "revision": self.revision,
            "disabled": self.disabled,
            "health": self.health.value if self.health is not None else None,
            "decision": self.decision.value,
            "active_approvals": [item.canonical() for item in self.active_approvals],
            "revoked_approvals": [item.canonical() for item in self.revoked_approvals],
            "evidence": [item.canonical() for item in self.evidence],
            "policy_references": [list(item) for item in self.policy_references],
            "missing_prerequisites": list(self.missing_prerequisites),
            "blocking_reason_codes": list(self.blocking_reason_codes),
            "review_requirements": list(self.review_requirements),
            "summary": self.summary,
            "consistency_boundary": self.consistency_boundary,
            "data_binding_status": self.data_binding_status,
            "dataset_status": self.dataset_status.value if self.dataset_status else None,
            "dataset_content_hash": self.dataset_content_hash,
            "dataset_manifest_hash": self.dataset_manifest_hash,
            "qualification_id": self.qualification_id,
            "qualification_status": (
                self.qualification_status.value if self.qualification_status else None
            ),
            "qualification_verdict": self.qualification_verdict,
            "empirical_eligibility": self.empirical_eligibility,
            "oos_data_status": self.oos_data_status,
        }


@dataclass(frozen=True)
class ProvenanceGraph:
    query_scope: str
    query_timestamp: datetime
    consistency_boundary: str
    governance_revision: int | None
    nodes: tuple[ProvenanceNode, ...]
    relationships: tuple[ProvenanceRelationship, ...]
    findings: tuple[ProvenanceFinding, ...]
    eligibility: ProvenanceEligibilityExplanation | None
    history_truncated: bool
    attachments_truncated: bool
    next_after_revision: int | None

    def __post_init__(self) -> None:
        require_utc(self.query_timestamp)

    def content_payload(self) -> dict[str, object]:
        """Stable export content, excluding generation-time query timestamps."""

        return {
            "schema_version": "provenance-graph.v1",
            "query_scope": self.query_scope,
            "consistency_boundary": self.consistency_boundary,
            "governance_revision": self.governance_revision,
            "nodes": [item.canonical() for item in self.nodes],
            "relationships": [item.canonical() for item in self.relationships],
            "findings": [item.canonical() for item in self.findings],
            "eligibility": self.eligibility.canonical() if self.eligibility else None,
            "history_truncated": self.history_truncated,
            "attachments_truncated": self.attachments_truncated,
            "next_after_revision": self.next_after_revision,
        }

    def canonical(self) -> dict[str, object]:
        return {
            **self.content_payload(),
            "query_timestamp": self.query_timestamp.isoformat(),
        }


@dataclass(frozen=True)
class ExperimentDataEligibilityExplanation:
    """Structured explanation of whether an experiment's data is eligible."""

    experiment_id: str
    integrity_status: IntegrityStatus
    binding_status: str
    dataset_id: str | None
    dataset_status: IntegrityStatus | None
    dataset_content_hash: str | None
    manifest_hash: str | None
    qualification_id: str | None
    qualification_status: IntegrityStatus | None
    qualification_verdict: str | None
    empirical_eligibility: str | None
    slice_status: IntegrityStatus | None
    oos_status: str
    reasons: tuple[str, ...]
    summary: str
    query_timestamp: datetime

    def __post_init__(self) -> None:
        require_utc(self.query_timestamp)

    def canonical(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "integrity_status": self.integrity_status.value,
            "binding_status": self.binding_status,
            "dataset_id": self.dataset_id,
            "dataset_status": self.dataset_status.value if self.dataset_status else None,
            "dataset_content_hash": self.dataset_content_hash,
            "manifest_hash": self.manifest_hash,
            "qualification_id": self.qualification_id,
            "qualification_status": (
                self.qualification_status.value if self.qualification_status else None
            ),
            "qualification_verdict": self.qualification_verdict,
            "empirical_eligibility": self.empirical_eligibility,
            "slice_status": self.slice_status.value if self.slice_status else None,
            "oos_status": self.oos_status,
            "reasons": list(self.reasons),
            "summary": self.summary,
            "query_timestamp": self.query_timestamp.isoformat(),
        }


@dataclass(frozen=True)
class ProvenanceExport:
    content: dict[str, object]
    generated_at: datetime
    content_digest: str

    def __post_init__(self) -> None:
        require_utc(self.generated_at)

    def to_json(self) -> str:
        payload = {
            "schema_version": "provenance-export.v1",
            "content": self.content,
            "generation_metadata": {"generated_at": self.generated_at.isoformat()},
            "content_digest": self.content_digest,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"


@dataclass(frozen=True)
class HistoricalEventExplanation:
    strategy_id: str
    strategy_version: str
    event_id: str
    integrity_status: IntegrityStatus
    event: LifecycleEvent | None
    policy_reference: tuple[str, str] | None
    evidence_id: str | None
    approval_id: str | None
    evidence_revoked_at_event: bool | None
    evidence_revoked_now: bool | None
    approval_revoked_at_event: bool | None
    approval_revoked_now: bool | None
    historical_reconstruction_complete: bool
    limitations: tuple[str, ...]
    summary: str
    query_timestamp: datetime

    def __post_init__(self) -> None:
        require_utc(self.query_timestamp)

    def canonical(self) -> dict[str, object]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "event_id": self.event_id,
            "integrity_status": self.integrity_status.value,
            "event": self.event.event_id if self.event else None,
            "policy_reference": list(self.policy_reference) if self.policy_reference else None,
            "evidence_id": self.evidence_id,
            "approval_id": self.approval_id,
            "evidence_revoked_at_event": self.evidence_revoked_at_event,
            "evidence_revoked_now": self.evidence_revoked_now,
            "approval_revoked_at_event": self.approval_revoked_at_event,
            "approval_revoked_now": self.approval_revoked_now,
            "historical_reconstruction_complete": self.historical_reconstruction_complete,
            "limitations": list(self.limitations),
            "summary": self.summary,
            "query_timestamp": self.query_timestamp.isoformat(),
        }


@dataclass(frozen=True)
class _ExperimentFacts:
    experiment_id: str
    experiment_hash: str
    schema_version: str
    dataset_version: str
    dataset_hash: str
    split_id: str
    scope: ResearchScope
    period: TemporalRange
    strategy_id: str
    strategy_version: str
    selection_eligible: bool
    frozen_from_experiment_id: str | None
    research_family_id: str | None
    candidate_id: str | None
    canonical_payload: dict[str, object]
    result_hash: str
    result_decision: ResearchDecision
    result_locked_oos_touched: bool
    result_contaminated: bool
    result_metrics: tuple[tuple[str, str | None], ...]
    result_notes: tuple[str, ...]
    input_binding: ExperimentInputBinding | None


@dataclass(frozen=True)
class _ExperimentRead:
    experiment_id: str
    status: IntegrityStatus
    facts: _ExperimentFacts | None
    failure_code: str | None = None


@dataclass(frozen=True)
class _InputVerification:
    integrity_status: IntegrityStatus
    binding_status: str
    dataset_status: IntegrityStatus
    qualification_status: IntegrityStatus
    qualification_verdict: str | None
    empirical_eligibility: str | None
    slice_status: IntegrityStatus
    reasons: tuple[str, ...]


class ExperimentProvenanceReader:
    """Strict adapter over the existing immutable experiment registry."""

    _EXPERIMENT_FIELDS = frozenset(
        {
            "dataset_version",
            "dataset_hash",
            "split_id",
            "scope",
            "period",
            "strategy_id",
            "strategy_version",
            "parameters",
            "feature_versions",
            "regime_configuration_id",
            "fusion_configuration_id",
            "risk_policy_configuration_id",
            "backtest_experiment_id",
            "cost_scenario_id",
            "code_version",
            "random_seed",
            "selection_eligible",
            "frozen_from_experiment_id",
            "research_family_id",
            "candidate_id",
        }
    )
    _RESULT_FIELDS = frozenset(
        {
            "experiment_id",
            "metrics",
            "decision",
            "locked_oos_touched",
            "selection_influenced_by_locked_oos",
            "notes",
            "result_hash",
        }
    )

    def __init__(self, registry: ExperimentRegistry) -> None:
        self._registry = registry

    def read(self, experiment_id: str) -> _ExperimentRead:
        try:
            payload = self._registry.read(experiment_id)
        except ResearchError as exc:
            return _ExperimentRead(
                experiment_id,
                _classify_registry_failure(exc),
                None,
                "EXPERIMENT_RECORD_UNREADABLE",
            )
        try:
            if set(payload) != {"schema_version", "experiment_id", "experiment", "result"}:
                raise ValueError("schema")
            schema_version = payload["schema_version"]
            if schema_version not in {"phase9-experiment.v1", "phase9-experiment.v2"}:
                raise ValueError("schema")
            if payload["experiment_id"] != experiment_id:
                raise ValueError("identity")
            experiment = payload["experiment"]
            result = payload["result"]
            expected_experiment_fields = self._EXPERIMENT_FIELDS | (
                {"input_binding"} if schema_version == "phase9-experiment.v2" else set()
            )
            if not isinstance(experiment, dict) or set(experiment) != expected_experiment_fields:
                raise ValueError("schema")
            if not isinstance(result, dict) or set(result) != self._RESULT_FIELDS:
                raise ValueError("schema")
            facts = self._parse(
                experiment_id,
                experiment,
                result,
                schema_version,
            )
        except ValueError as exc:
            status = (
                IntegrityStatus.HASH_MISMATCH
                if str(exc) in {"identity", "hash"}
                else IntegrityStatus.INVALID_SCHEMA
            )
            return _ExperimentRead(
                experiment_id, status, None, "EXPERIMENT_SCHEMA_OR_IDENTITY_INVALID"
            )
        except (TypeError, ResearchError):
            return _ExperimentRead(
                experiment_id,
                IntegrityStatus.INVALID_SCHEMA,
                None,
                "EXPERIMENT_SCHEMA_INVALID",
            )
        return _ExperimentRead(experiment_id, IntegrityStatus.VERIFIED, facts)

    def _parse(
        self,
        experiment_id: str,
        experiment: dict[str, object],
        result: dict[str, object],
        schema_version: str,
    ) -> _ExperimentFacts:
        required_text = (
            "dataset_version",
            "dataset_hash",
            "split_id",
            "strategy_id",
            "strategy_version",
            "backtest_experiment_id",
            "cost_scenario_id",
            "code_version",
        )
        for field in required_text:
            value = experiment[field]
            if not isinstance(value, str) or not value.strip():
                raise ValueError("schema")
        if type(experiment["selection_eligible"]) is not bool:
            raise ValueError("schema")
        period_payload = experiment["period"]
        if not isinstance(period_payload, dict) or set(period_payload) != {"start", "end"}:
            raise ValueError("schema")
        try:
            period = TemporalRange(
                datetime.fromisoformat(cast(str, period_payload["start"])),
                datetime.fromisoformat(cast(str, period_payload["end"])),
            )
            scope = ResearchScope(cast(str, experiment["scope"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("schema") from exc
        for field in ("random_seed",):
            if experiment[field] is not None and type(experiment[field]) is not int:
                raise ValueError("schema")
        optional_text = (
            "frozen_from_experiment_id",
            "research_family_id",
            "candidate_id",
            "regime_configuration_id",
            "fusion_configuration_id",
            "risk_policy_configuration_id",
        )
        for field in optional_text:
            if experiment[field] is not None and not isinstance(experiment[field], str):
                raise ValueError("schema")
        parameters = experiment["parameters"]
        if not isinstance(parameters, list) or any(
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or (item[1] is not None and not isinstance(item[1], str))
            for item in parameters
        ):
            raise ValueError("schema")
        feature_versions = experiment["feature_versions"]
        if not isinstance(feature_versions, list) or any(
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or type(item[1]) is not int
            for item in feature_versions
        ):
            raise ValueError("schema")
        if result["experiment_id"] != experiment_id:
            raise ValueError("schema")
        metrics_payload = result["metrics"]
        notes_payload = result["notes"]
        if not isinstance(metrics_payload, list) or not isinstance(notes_payload, list):
            raise ValueError("schema")
        metrics: tuple[tuple[str, str | None], ...] = tuple(
            (item[0], item[1])
            for item in metrics_payload
            if isinstance(item, list)
            and len(item) == 2
            and isinstance(item[0], str)
            and (item[1] is None or isinstance(item[1], str))
        )
        if len(metrics) != len(metrics_payload) or any(not name.strip() for name, _ in metrics):
            raise ValueError("schema")
        if any(not isinstance(item, str) for item in notes_payload):
            raise ValueError("schema")
        if (
            type(result["locked_oos_touched"]) is not bool
            or type(result["selection_influenced_by_locked_oos"]) is not bool
        ):
            raise ValueError("schema")
        try:
            decision = ResearchDecision(cast(str, result["decision"]))
            parsed_result = ExperimentResult(
                experiment_id=experiment_id,
                metrics=metrics,
                decision=decision,
                locked_oos_touched=result["locked_oos_touched"],
                selection_influenced_by_locked_oos=result["selection_influenced_by_locked_oos"],
                notes=tuple(cast(list[str], notes_payload)),
            )
        except (TypeError, ValueError, ResearchError) as exc:
            raise ValueError("schema") from exc
        if result["result_hash"] != parsed_result.result_hash:
            raise ValueError("hash")
        experiment_hash = hashlib.sha256(_canonical_bytes(experiment)).hexdigest()
        expected_id = "research-" + experiment_hash[:24]
        if expected_id != experiment_id:
            raise ValueError("identity")
        if scope is ResearchScope.LOCKED_OUT_OF_SAMPLE and experiment["selection_eligible"]:
            raise ValueError("schema")
        if (scope is ResearchScope.LOCKED_OUT_OF_SAMPLE) != result["locked_oos_touched"]:
            raise ValueError("schema")
        if (experiment["research_family_id"] is None) != (experiment["candidate_id"] is None):
            raise ValueError("schema")
        input_binding = None
        if schema_version == "phase9-experiment.v2":
            try:
                input_binding = ExperimentInputBinding.from_canonical(experiment["input_binding"])
            except ResearchError as exc:
                raise ValueError("schema") from exc
        return _ExperimentFacts(
            experiment_id=experiment_id,
            experiment_hash=experiment_hash,
            schema_version=schema_version,
            dataset_version=cast(str, experiment["dataset_version"]),
            dataset_hash=cast(str, experiment["dataset_hash"]),
            split_id=cast(str, experiment["split_id"]),
            scope=scope,
            period=period,
            strategy_id=cast(str, experiment["strategy_id"]),
            strategy_version=cast(str, experiment["strategy_version"]),
            selection_eligible=experiment["selection_eligible"],
            frozen_from_experiment_id=cast(str | None, experiment["frozen_from_experiment_id"]),
            research_family_id=cast(str | None, experiment["research_family_id"]),
            candidate_id=cast(str | None, experiment["candidate_id"]),
            canonical_payload=experiment,
            result_hash=parsed_result.result_hash,
            result_decision=parsed_result.decision,
            result_locked_oos_touched=parsed_result.locked_oos_touched,
            result_contaminated=parsed_result.selection_influenced_by_locked_oos,
            result_metrics=parsed_result.metrics,
            result_notes=parsed_result.notes,
            input_binding=input_binding,
        )


@dataclass(frozen=True)
class _StoredEvidenceRead:
    evidence_id: str
    evidence_hash: str
    artifact_hash: str
    attached_at: datetime | None
    value: EvidenceAttachment | None
    status: IntegrityStatus


@dataclass(frozen=True)
class _StoredApprovalRead:
    approval_id: str
    approval_hash: str
    artifact_hash: str
    evidence_id: str
    approved_at: datetime | None
    value: ApprovalRecord | None
    status: IntegrityStatus


@dataclass(frozen=True)
class _StoredEventRead:
    event_id: str
    value: LifecycleEvent | None
    status: IntegrityStatus


@dataclass(frozen=True)
class _StoredRevocationRead:
    revocation_id: str
    target_type: str
    value: RevocationRecord | None
    status: IntegrityStatus


@dataclass(frozen=True)
class _DatabaseSnapshot:
    query_timestamp: datetime
    state: StrategyState | None
    state_status: IntegrityStatus
    artifact: StrategyArtifact | None
    artifact_status: IntegrityStatus
    artifact_hash: str | None
    evidence: tuple[_StoredEvidenceRead, ...]
    approvals: tuple[_StoredApprovalRead, ...]
    evidence_revocations: tuple[_StoredRevocationRead, ...]
    approval_revocations: tuple[_StoredRevocationRead, ...]
    events: tuple[_StoredEventRead, ...]
    history_truncated: bool
    attachments_truncated: bool
    next_after_revision: int | None


class GovernanceProvenanceReader:
    """Read existing governance tables without using mutation methods."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def read_strategy(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        history_limit: int,
        after_revision: int,
        evidence_limit: int,
        approval_limit: int,
        event_id: str | None = None,
    ) -> _DatabaseSnapshot:
        if (
            not 1 <= history_limit <= 500
            or not 1 <= evidence_limit <= 500
            or not 1 <= approval_limit <= 500
        ):
            raise ProvenanceError("provenance page limits must be between 1 and 500")
        if after_revision < 0:
            raise ProvenanceError("provenance revision cursor must be non-negative")
        query_timestamp = datetime.now(UTC)
        try:
            connection = self._engine.connect()
            if self._engine.dialect.name == "postgresql":
                connection = connection.execution_options(isolation_level="REPEATABLE READ")
            with connection.begin():
                return self._read_connection(
                    connection,
                    query_timestamp=query_timestamp,
                    strategy_id=strategy_id,
                    strategy_version=strategy_version,
                    history_limit=history_limit,
                    after_revision=after_revision,
                    evidence_limit=evidence_limit,
                    approval_limit=approval_limit,
                    event_id=event_id,
                )
        except SQLAlchemyError as exc:
            raise ProvenanceError("provenance database read failed") from exc
        finally:
            if "connection" in locals():
                connection.close()

    def _read_connection(
        self,
        connection: Connection,
        *,
        query_timestamp: datetime,
        strategy_id: str,
        strategy_version: str,
        history_limit: int,
        after_revision: int,
        evidence_limit: int,
        approval_limit: int,
        event_id: str | None,
    ) -> _DatabaseSnapshot:
        state_row = (
            connection.execute(
                select(strategy_lifecycle_state).where(
                    strategy_lifecycle_state.c.strategy_id == strategy_id,
                    strategy_lifecycle_state.c.strategy_version == strategy_version,
                )
            )
            .mappings()
            .first()
        )
        if state_row is None:
            return _DatabaseSnapshot(
                query_timestamp,
                None,
                IntegrityStatus.MISSING,
                None,
                IntegrityStatus.MISSING,
                None,
                (),
                (),
                (),
                (),
                (),
                False,
                False,
                None,
            )
        state, state_status = self._parse_state(state_row)
        artifact_hash = self._text_or_none(state_row.get("artifact_hash"))
        artifact = None
        artifact_status = IntegrityStatus.MISSING
        if artifact_hash is not None:
            artifact_row = (
                connection.execute(
                    select(strategy_artifacts).where(
                        strategy_artifacts.c.artifact_hash == artifact_hash
                    )
                )
                .mappings()
                .first()
            )
            if artifact_row is None:
                artifact_status = IntegrityStatus.MISSING
            else:
                artifact, artifact_status = self._parse_artifact(artifact_row)
                if artifact is not None and (
                    artifact.strategy_id != strategy_id
                    or artifact.strategy_version != strategy_version
                ):
                    artifact = None
                    artifact_status = IntegrityStatus.HASH_MISMATCH
        evidence_rows = (
            connection.execute(
                select(strategy_evidence)
                .where(strategy_evidence.c.artifact_hash == artifact_hash)
                .order_by(
                    strategy_evidence.c.attached_at.desc(), strategy_evidence.c.evidence_id.desc()
                )
                .limit(evidence_limit + 1)
            )
            .mappings()
            .all()
        )
        attachments_truncated = len(evidence_rows) > evidence_limit
        evidence = tuple(self._parse_evidence(row) for row in evidence_rows[:evidence_limit])
        evidence_ids = [item.evidence_id for item in evidence]
        approval_rows = (
            connection.execute(
                select(strategy_approvals)
                .where(strategy_approvals.c.artifact_hash == artifact_hash)
                .order_by(
                    strategy_approvals.c.approved_at.desc(), strategy_approvals.c.approval_id.desc()
                )
                .limit(approval_limit + 1)
            )
            .mappings()
            .all()
        )
        attachments_truncated = attachments_truncated or len(approval_rows) > approval_limit
        approvals = tuple(self._parse_approval(row) for row in approval_rows[:approval_limit])
        approval_ids = [item.approval_id for item in approvals]
        evidence_revocation_rows = (
            connection.execute(
                select(strategy_evidence_revocations).where(
                    strategy_evidence_revocations.c.evidence_id.in_(evidence_ids)
                )
            )
            .mappings()
            .all()
            if evidence_ids
            else []
        )
        approval_revocation_rows = (
            connection.execute(
                select(strategy_approval_revocations).where(
                    strategy_approval_revocations.c.approval_id.in_(approval_ids)
                )
            )
            .mappings()
            .all()
            if approval_ids
            else []
        )
        event_statement = select(strategy_lifecycle_events).where(
            strategy_lifecycle_events.c.strategy_id == strategy_id,
            strategy_lifecycle_events.c.strategy_version == strategy_version,
        )
        if event_id is not None:
            event_statement = event_statement.where(
                strategy_lifecycle_events.c.event_id == event_id
            )
        else:
            event_statement = event_statement.where(
                strategy_lifecycle_events.c.resulting_revision > after_revision
            )
        event_rows = (
            connection.execute(
                event_statement.order_by(strategy_lifecycle_events.c.resulting_revision).limit(
                    history_limit + 1
                )
            )
            .mappings()
            .all()
        )
        history_truncated = event_id is None and len(event_rows) > history_limit
        event_values = tuple(self._parse_event(row) for row in event_rows[:history_limit])
        next_after_revision = (
            event_values[-1].value.resulting_revision
            if history_truncated and event_values and event_values[-1].value is not None
            else None
        )
        return _DatabaseSnapshot(
            query_timestamp,
            state,
            state_status,
            artifact,
            artifact_status,
            artifact_hash,
            evidence,
            approvals,
            tuple(self._parse_revocation(row, "evidence") for row in evidence_revocation_rows),
            tuple(self._parse_revocation(row, "approval") for row in approval_revocation_rows),
            event_values,
            history_truncated,
            attachments_truncated,
            next_after_revision,
        )

    @staticmethod
    def _text_or_none(value: object) -> str | None:
        return value if isinstance(value, str) else None

    @staticmethod
    def _parse_state(row: Mapping[Any, Any]) -> tuple[StrategyState | None, IntegrityStatus]:
        try:
            updated_at = row["updated_at"]
            if not isinstance(updated_at, datetime):
                raise ValueError
            state = StrategyState(
                strategy_id=cast(str, row["strategy_id"]),
                strategy_version=cast(str, row["strategy_version"]),
                artifact_hash=cast(str, row["artifact_hash"]),
                stage=StrategyStage(cast(str, row["stage"])),
                health=StrategyHealth(cast(str, row["health"])),
                revision=cast(int, row["revision"]),
                disabled=cast(bool, row["disabled"]),
                disable_reason=cast(str | None, row["disable_reason"]),
                updated_at=_utc(updated_at),
                last_event_id=cast(str | None, row["last_event_id"]),
            )
        except (TypeError, ValueError, LifecycleError):
            return None, IntegrityStatus.INVALID_SCHEMA
        return state, IntegrityStatus.VERIFIED

    @staticmethod
    def _parse_artifact(
        row: Mapping[Any, Any],
    ) -> tuple[StrategyArtifact | None, IntegrityStatus]:
        try:
            artifact = StrategyArtifact.from_canonical(
                row["canonical_payload"], expected_hash=cast(str, row["artifact_hash"])
            )
            if (
                artifact.strategy_id != row["strategy_id"]
                or artifact.strategy_version != row["strategy_version"]
            ):
                raise LifecycleError("artifact hash mismatch")
            registered_at = row["registered_at"]
            if not isinstance(registered_at, datetime):
                raise ValueError
            return artifact, IntegrityStatus.VERIFIED
        except LifecycleError as exc:
            status = (
                IntegrityStatus.HASH_MISMATCH
                if "hash mismatch" in str(exc)
                else IntegrityStatus.INVALID_SCHEMA
            )
            return None, status
        except (TypeError, ValueError):
            return None, IntegrityStatus.INVALID_SCHEMA

    @staticmethod
    def _parse_evidence(row: Mapping[Any, Any]) -> _StoredEvidenceRead:
        try:
            attached_at = row["attached_at"]
            if not isinstance(attached_at, datetime):
                raise ValueError
            value = EvidenceAttachment.from_canonical(
                row["canonical_payload"], expected_hash=cast(str, row["evidence_hash"])
            )
            if (
                value.evidence_id != row["evidence_id"]
                or value.artifact_hash != row["artifact_hash"]
            ):
                raise LifecycleError("evidence hash mismatch")
            return _StoredEvidenceRead(
                cast(str, row["evidence_id"]),
                cast(str, row["evidence_hash"]),
                cast(str, row["artifact_hash"]),
                _utc(attached_at),
                value,
                IntegrityStatus.VERIFIED,
            )
        except LifecycleError as exc:
            status = (
                IntegrityStatus.HASH_MISMATCH
                if "hash mismatch" in str(exc)
                else IntegrityStatus.INVALID_SCHEMA
            )
        except (TypeError, ValueError):
            status = IntegrityStatus.INVALID_SCHEMA
        return _StoredEvidenceRead(
            cast(str, row["evidence_id"]),
            cast(str, row["evidence_hash"]),
            cast(str, row["artifact_hash"]),
            _utc(row["attached_at"]) if isinstance(row["attached_at"], datetime) else None,
            None,
            status,
        )

    @staticmethod
    def _parse_approval(row: Mapping[Any, Any]) -> _StoredApprovalRead:
        try:
            approved_at = row["approved_at"]
            if not isinstance(approved_at, datetime):
                raise ValueError
            value = ApprovalRecord.from_canonical(row["canonical_payload"])
            if (
                value.approval_hash != row["approval_hash"]
                or value.approval_id != row["approval_id"]
                or value.artifact_hash != row["artifact_hash"]
                or value.evidence_id != row["evidence_id"]
            ):
                raise LifecycleError("approval hash mismatch")
            return _StoredApprovalRead(
                cast(str, row["approval_id"]),
                cast(str, row["approval_hash"]),
                cast(str, row["artifact_hash"]),
                cast(str, row["evidence_id"]),
                _utc(approved_at),
                value,
                IntegrityStatus.VERIFIED,
            )
        except LifecycleError as exc:
            status = (
                IntegrityStatus.HASH_MISMATCH
                if "hash mismatch" in str(exc)
                else IntegrityStatus.INVALID_SCHEMA
            )
        except (TypeError, ValueError):
            status = IntegrityStatus.INVALID_SCHEMA
        return _StoredApprovalRead(
            cast(str, row["approval_id"]),
            cast(str, row["approval_hash"]),
            cast(str, row["artifact_hash"]),
            cast(str, row["evidence_id"]),
            _utc(row["approved_at"]) if isinstance(row["approved_at"], datetime) else None,
            None,
            status,
        )

    @staticmethod
    def _parse_event(row: Mapping[Any, Any]) -> _StoredEventRead:
        try:
            occurred_at = row["occurred_at"]
            if not isinstance(occurred_at, datetime):
                raise ValueError
            value = LifecycleEvent(
                event_id=cast(str, row["event_id"]),
                strategy_id=cast(str, row["strategy_id"]),
                strategy_version=cast(str, row["strategy_version"]),
                artifact_hash=cast(str, row["artifact_hash"]),
                event_type=cast(str, row["event_type"]),
                previous_stage=StrategyStage(cast(str, row["previous_stage"])),
                resulting_stage=StrategyStage(cast(str, row["resulting_stage"])),
                expected_revision=cast(int, row["expected_revision"]),
                resulting_revision=cast(int, row["resulting_revision"]),
                actor_id=cast(str, row["actor_id"]),
                policy_id=cast(str, row["policy_id"]),
                policy_version=cast(str, row["policy_version"]),
                evidence_id=cast(str | None, row["evidence_id"]),
                approval_id=cast(str | None, row["approval_id"]),
                reason_code=cast(str, row["reason_code"]),
                reason=cast(str, row["reason"]),
                occurred_at=_utc(occurred_at),
                idempotency_key=cast(str, row["idempotency_key"]),
                correlation_id=cast(str, row["correlation_id"]),
            )
            return _StoredEventRead(value.event_id, value, IntegrityStatus.VERIFIED)
        except (TypeError, ValueError, LifecycleError):
            identity = row.get("event_id")
            return _StoredEventRead(
                identity if isinstance(identity, str) else "unknown",
                None,
                IntegrityStatus.INVALID_SCHEMA,
            )

    @staticmethod
    def _parse_revocation(row: Mapping[Any, Any], target_type: str) -> _StoredRevocationRead:
        identity = row.get("revocation_id")
        revocation_id = identity if isinstance(identity, str) else "unknown"
        try:
            revoked_at = row["revoked_at"]
            if not isinstance(revoked_at, datetime):
                raise ValueError
            target_id = row["evidence_id"] if target_type == "evidence" else row["approval_id"]
            value = RevocationRecord(
                revocation_id=cast(str, row["revocation_id"]),
                target_type=target_type,
                target_id=cast(str, target_id),
                actor_id=cast(str, row["actor_id"]),
                reason=cast(str, row["reason"]),
                revoked_at=_utc(revoked_at),
                idempotency_key=cast(str, row["idempotency_key"]),
            )
        except (TypeError, ValueError, LifecycleError):
            return _StoredRevocationRead(
                revocation_id, target_type, None, IntegrityStatus.INVALID_SCHEMA
            )
        return _StoredRevocationRead(revocation_id, target_type, value, IntegrityStatus.VERIFIED)


class _GraphBuilder:
    _STATUS_ORDER = {
        IntegrityStatus.VERIFIED: 0,
        IntegrityStatus.UNSUPPORTED: 1,
        IntegrityStatus.MISSING: 2,
        IntegrityStatus.INVALID_SCHEMA: 3,
        IntegrityStatus.HASH_MISMATCH: 4,
        IntegrityStatus.UNAUTHORIZED: 5,
    }

    def __init__(self) -> None:
        self.nodes: dict[str, ProvenanceNode] = {}
        self.relationships: dict[str, ProvenanceRelationship] = {}
        self.findings: dict[tuple[str, str], ProvenanceFinding] = {}

    def add_node(self, node: ProvenanceNode) -> None:
        existing = self.nodes.get(node.node_id)
        if existing is None:
            self.nodes[node.node_id] = node
            return
        if (
            existing.content_hash != node.content_hash
            and existing.content_hash
            and node.content_hash
        ):
            self.add_finding(
                "CONFLICTING_CONTENT_REFERENCES",
                IntegrityStatus.HASH_MISMATCH,
                node.entity_type,
                node.stable_identity,
            )
        if (
            self._STATUS_ORDER[node.integrity_status]
            > self._STATUS_ORDER[existing.integrity_status]
        ):
            self.nodes[node.node_id] = node

    def add_relationship(self, relationship: ProvenanceRelationship) -> None:
        self.relationships[relationship.relationship_id] = relationship

    def add_finding(
        self,
        code: str,
        status: IntegrityStatus,
        entity_type: ProvenanceNodeType,
        stable_identity: str,
    ) -> None:
        self.findings[(code, _node_id(entity_type, stable_identity))] = ProvenanceFinding(
            code, status, entity_type, stable_identity
        )

    def cycle_nodes(self) -> tuple[str, ...]:
        """Return nodes participating in a directed relationship cycle."""

        adjacency: dict[str, set[str]] = {}
        for relationship in self.relationships.values():
            adjacency.setdefault(relationship.source_node_id, set()).add(
                relationship.target_node_id
            )
        visiting: set[str] = set()
        visited: set[str] = set()
        cycle: set[str] = set()

        def visit(node_id: str, path: tuple[str, ...]) -> None:
            if node_id in visiting:
                cycle.update(path[path.index(node_id) :])
                return
            if node_id in visited:
                return
            visiting.add(node_id)
            for target in sorted(adjacency.get(node_id, ())):
                visit(target, (*path, node_id))
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in sorted(adjacency):
            visit(node_id, ())
        return tuple(sorted(cycle))


class ProvenanceQueryService:
    """Deterministic, read-only provenance and eligibility query service."""

    _CONSISTENCY = "database repeatable-read snapshot plus immutable local experiment reads"

    def __init__(
        self,
        store: StrategyGovernanceStore,
        experiment_registry: ExperimentRegistry,
        actors: ActorDirectory,
        *,
        clock: Callable[[], datetime] | None = None,
        dataset_reader: DatasetRecordReader | None = None,
        qualification_registry: DatasetQualificationRegistry | None = None,
    ) -> None:
        self._store = store
        self._experiment_reader = ExperimentProvenanceReader(experiment_registry)
        self._actors = actors
        self._clock = clock or (lambda: datetime.now(UTC))
        self._database_reader = GovernanceProvenanceReader(store.engine)
        self._dataset_reader = dataset_reader
        self._qualification_registry = qualification_registry

    def get_strategy_provenance(
        self,
        strategy_id: str,
        strategy_version: str,
        *,
        actor_id: str,
        history_limit: int = 100,
        after_revision: int = 0,
        evidence_limit: int = 100,
        approval_limit: int = 100,
    ) -> ProvenanceGraph:
        self._authorize(actor_id)
        snapshot = self._database_reader.read_strategy(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            history_limit=history_limit,
            after_revision=after_revision,
            evidence_limit=evidence_limit,
            approval_limit=approval_limit,
        )
        return self._build_graph(
            snapshot,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            query_scope=f"strategy:{strategy_id}@{strategy_version}",
        )

    def get_experiment_provenance(self, experiment_id: str, *, actor_id: str) -> ProvenanceGraph:
        self._authorize(actor_id)
        read = self._experiment_reader.read(experiment_id)
        builder = _GraphBuilder()
        self._add_experiment(builder, read)
        if read.facts is not None:
            artifact_identity = f"{read.facts.strategy_id}@{read.facts.strategy_version}"
            builder.add_node(
                ProvenanceNode(
                    ProvenanceNodeType.STRATEGY_ARTIFACT,
                    "strategy-artifact-reference.v1",
                    artifact_identity,
                    None,
                    (),
                    IntegrityStatus.UNSUPPORTED,
                    "strategy_artifacts",
                    EvidenceClassification.UNKNOWN,
                )
            )
            builder.add_finding(
                "EXPERIMENT_DOES_NOT_EMBED_ARTIFACT_HASH",
                IntegrityStatus.UNSUPPORTED,
                ProvenanceNodeType.STRATEGY_ARTIFACT,
                artifact_identity,
            )
        query_timestamp = self._now()
        return ProvenanceGraph(
            query_scope=f"experiment:{experiment_id}",
            query_timestamp=query_timestamp,
            consistency_boundary=(
                "immutable local experiment, dataset, and qualification registry reads"
                if read.facts is not None and read.facts.input_binding is not None
                else "immutable local experiment registry read"
            ),
            governance_revision=None,
            nodes=tuple(sorted(builder.nodes.values(), key=lambda item: item.node_id)),
            relationships=tuple(
                sorted(builder.relationships.values(), key=lambda item: item.relationship_id)
            ),
            findings=tuple(
                sorted(
                    builder.findings.values(), key=lambda item: (item.code, item.stable_identity)
                )
            ),
            eligibility=None,
            history_truncated=False,
            attachments_truncated=False,
            next_after_revision=None,
        )

    def get_dataset_provenance(self, dataset_id: str, *, actor_id: str) -> ProvenanceGraph:
        """Return verified dataset, admission, raw, and qualification facts."""

        self._authorize(actor_id)
        builder = _GraphBuilder()
        if self._dataset_reader is None:
            self._add_unavailable_dataset(builder, dataset_id, IntegrityStatus.UNSUPPORTED)
        else:
            read = self._dataset_reader.read(dataset_id)
            self._add_dataset_record(builder, read)
        return self._graph_from_builder(
            builder,
            query_scope=f"dataset:{dataset_id}",
            consistency_boundary="immutable local dataset registry read",
        )

    def get_qualification_provenance(
        self, qualification_id: str, *, actor_id: str
    ) -> ProvenanceGraph:
        """Return one qualification and its explicitly referenced dataset."""

        self._authorize(actor_id)
        builder = _GraphBuilder()
        if self._qualification_registry is None:
            self._add_unavailable_qualification(builder, qualification_id)
        else:
            read = self._qualification_registry.read(qualification_id)
            self._add_qualification(builder, read)
            if read.record is not None and self._dataset_reader is not None:
                self._add_dataset_record(
                    builder, self._dataset_reader.read(read.record.dataset_id)
                )
                self._bind_qualification_to_dataset(builder, read.record)
        return self._graph_from_builder(
            builder,
            query_scope=f"qualification:{qualification_id}",
            consistency_boundary="immutable local dataset and qualification registry reads",
        )

    def explain_experiment_data_eligibility(
        self, experiment_id: str, *, actor_id: str
    ) -> ExperimentDataEligibilityExplanation:
        """Explain dataset qualification without evaluating strategy promotion."""

        self._authorize(actor_id)
        read = self._experiment_reader.read(experiment_id)
        if read.facts is None:
            return ExperimentDataEligibilityExplanation(
                experiment_id,
                read.status,
                "UNREADABLE",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                "UNKNOWN",
                (read.failure_code or "EXPERIMENT_UNAVAILABLE",),
                "Experiment data eligibility is unavailable.",
                self._now(),
            )
        binding = read.facts.input_binding
        if binding is None:
            return ExperimentDataEligibilityExplanation(
                experiment_id,
                IntegrityStatus.UNSUPPORTED,
                "LEGACY_UNBOUND",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                "LEGACY_UNBOUND",
                ("EXPERIMENT_INPUT_BINDING_MISSING",),
                "Experiment is a legacy record without an immutable input binding.",
                self._now(),
            )
        summary = self._verify_input_binding(read.facts)
        return ExperimentDataEligibilityExplanation(
                experiment_id,
                summary.integrity_status,
                summary.binding_status,
                binding.dataset_id,
                summary.dataset_status,
                binding.dataset_content_hash,
                binding.manifest_hash,
                binding.qualification_id,
                summary.qualification_status,
                summary.qualification_verdict,
                summary.empirical_eligibility,
                summary.slice_status,
                "CONTAMINATED" if read.facts.result_contaminated else "PRISTINE",
                summary.reasons,
                self._input_summary_text(summary.reasons),
                self._now(),
            )

    @staticmethod
    def _dataset_status(status: DatasetRecordStatus) -> IntegrityStatus:
        return IntegrityStatus(status.value)

    def _verify_input_binding(self, facts: _ExperimentFacts) -> _InputVerification:
        binding = facts.input_binding
        if binding is None:
            raise ProvenanceError("input verification requires a binding")
        dataset_read = (
            self._dataset_reader.read(binding.dataset_id)
            if self._dataset_reader is not None
            else None
        )
        qualification_read = (
            self._qualification_registry.read(binding.qualification_id)
            if self._qualification_registry is not None
            else None
        )
        dataset_status = (
            self._dataset_status(dataset_read.status)
            if dataset_read is not None
            else IntegrityStatus.UNSUPPORTED
        )
        qualification_status = (
            self._dataset_status(qualification_read.status)
            if qualification_read is not None
            else IntegrityStatus.UNSUPPORTED
        )
        verdict = (
            qualification_read.record.verdict.value
            if qualification_read is not None and qualification_read.record is not None
            else None
        )
        eligibility = (
            qualification_read.record.empirical_eligibility.value
            if qualification_read is not None and qualification_read.record is not None
            else None
        )
        slice_status = IntegrityStatus.UNSUPPORTED
        reasons: list[str] = []
        if dataset_read is None:
            reasons.append("DATASET_VERIFICATION_UNAVAILABLE")
        elif dataset_read.record is None:
            reasons.append(
                {
                    IntegrityStatus.MISSING: "DATASET_RECORD_MISSING",
                    IntegrityStatus.INVALID_SCHEMA: "DATASET_RECORD_INVALID",
                    IntegrityStatus.HASH_MISMATCH: "DATASET_RECORD_HASH_MISMATCH",
                    IntegrityStatus.UNSUPPORTED: "DATASET_VERIFICATION_UNAVAILABLE",
                }.get(dataset_status, "DATASET_RECORD_UNVERIFIED")
            )
        else:
            if dataset_read.status is not DatasetRecordStatus.VERIFIED:
                reasons.append(
                    {
                        IntegrityStatus.MISSING: "DATASET_RECORD_MISSING",
                        IntegrityStatus.INVALID_SCHEMA: "DATASET_RECORD_INVALID",
                        IntegrityStatus.HASH_MISMATCH: "DATASET_RECORD_HASH_MISMATCH",
                        IntegrityStatus.UNSUPPORTED: "DATASET_VERIFICATION_UNAVAILABLE",
                    }.get(dataset_status, "DATASET_RECORD_UNVERIFIED")
                )
            record = dataset_read.record
            if record.content_hash != binding.dataset_content_hash:
                reasons.append("DATASET_CONTENT_HASH_MISMATCH")
            if record.manifest_hash != binding.manifest_hash:
                reasons.append("DATASET_MANIFEST_HASH_MISMATCH")
            if self._dataset_reader is not None:
                slice_check = self._dataset_reader.verify_slice(dataset_read, binding)
                slice_status = self._dataset_status(slice_check.status)
                if slice_check.code is not None:
                    reasons.append(slice_check.code)
        if qualification_read is None:
            reasons.append("QUALIFICATION_VERIFICATION_UNAVAILABLE")
        elif qualification_read.record is None:
            reasons.append(
                {
                    IntegrityStatus.MISSING: "QUALIFICATION_MISSING",
                    IntegrityStatus.INVALID_SCHEMA: "QUALIFICATION_INVALID",
                    IntegrityStatus.HASH_MISMATCH: "QUALIFICATION_HASH_MISMATCH",
                    IntegrityStatus.UNSUPPORTED: "QUALIFICATION_VERIFICATION_UNAVAILABLE",
                }.get(qualification_status, "QUALIFICATION_UNVERIFIED")
            )
        else:
            qualification = qualification_read.record
            if (
                qualification.dataset_id != binding.dataset_id
                or qualification.dataset_content_hash != binding.dataset_content_hash
                or qualification.manifest_hash != binding.manifest_hash
            ):
                reasons.append("QUALIFICATION_DATASET_MISMATCH")
            if (
                qualification.policy_id != binding.qualification_policy_id
                or qualification.policy_version != binding.qualification_policy_version
            ):
                reasons.append("QUALIFICATION_POLICY_MISMATCH")
            if qualification.verdict is not DatasetQualificationStatus.QUALIFIED:
                reasons.append("DATASET_QUALIFICATION_REJECTED")
            if not binding.fixture and (
                qualification.fixture
                or qualification.empirical_eligibility
                is not ResearchDatasetEligibility.EMPIRICALLY_QUALIFIED_DATASET
            ):
                reasons.append("EMPIRICAL_ELIGIBILITY_BLOCKED")
            if binding.fixture and not qualification.fixture:
                reasons.append("FIXTURE_CLASSIFICATION_MISMATCH")
        if facts.result_contaminated or (
            facts.scope is ResearchScope.LOCKED_OUT_OF_SAMPLE
            and not facts.result_locked_oos_touched
        ):
            reasons.append("OOS_DATA_CONTAMINATED")
        unique_reasons = tuple(dict.fromkeys(reasons))
        if any(
            status in {IntegrityStatus.HASH_MISMATCH, IntegrityStatus.INVALID_SCHEMA}
            for status in (dataset_status, qualification_status, slice_status)
        ) or "OOS_DATA_CONTAMINATED" in unique_reasons:
            overall = IntegrityStatus.HASH_MISMATCH if any(
                status is IntegrityStatus.HASH_MISMATCH
                for status in (dataset_status, qualification_status, slice_status)
            ) else IntegrityStatus.INVALID_SCHEMA
        elif unique_reasons:
            overall = IntegrityStatus.MISSING if any(
                status is IntegrityStatus.MISSING
                for status in (dataset_status, qualification_status, slice_status)
            ) else IntegrityStatus.UNSUPPORTED
        else:
            overall = IntegrityStatus.VERIFIED
        return _InputVerification(
            overall,
            "BOUND_VERIFIED" if not unique_reasons else "BOUND_BLOCKED",
            dataset_status,
            qualification_status,
            verdict,
            eligibility,
            slice_status,
            unique_reasons,
        )

    @staticmethod
    def _input_summary_text(reasons: tuple[str, ...]) -> str:
        if not reasons:
            return "Experiment data inputs are integrity-verified and qualification-bound."
        return "Experiment data eligibility is blocked: " + ", ".join(reasons) + "."

    @staticmethod
    def _add_unavailable_dataset(
        builder: _GraphBuilder, dataset_id: str, status: IntegrityStatus
    ) -> None:
        builder.add_node(
            ProvenanceNode(
                ProvenanceNodeType.DATASET,
                "dataset-record.v1",
                dataset_id,
                None,
                (),
                status,
                "dataset_record_registry",
                EvidenceClassification.UNKNOWN,
            )
        )
        builder.add_finding(
            "DATASET_RECORD_UNAVAILABLE", status, ProvenanceNodeType.DATASET, dataset_id
        )

    @staticmethod
    def _add_unavailable_qualification(builder: _GraphBuilder, qualification_id: str) -> None:
        builder.add_node(
            ProvenanceNode(
                ProvenanceNodeType.DATASET_QUALIFICATION,
                "dataset-qualification.v1",
                qualification_id,
                qualification_id,
                (),
                IntegrityStatus.UNSUPPORTED,
                "dataset_qualification_registry",
                EvidenceClassification.UNKNOWN,
            )
        )
        builder.add_finding(
            "QUALIFICATION_REGISTRY_UNAVAILABLE",
            IntegrityStatus.UNSUPPORTED,
            ProvenanceNodeType.DATASET_QUALIFICATION,
            qualification_id,
        )

    def _add_dataset_record(self, builder: _GraphBuilder, read: DatasetRecordRead) -> None:
        record = read.record
        if record is None:
            self._add_unavailable_dataset(
                builder, read.dataset_id, self._dataset_status(read.status)
            )
            if read.failure_code:
                builder.add_finding(
                    read.failure_code,
                    self._dataset_status(read.status),
                    ProvenanceNodeType.DATASET,
                    read.dataset_id,
                )
            return
        classification = EvidenceClassification.UNKNOWN
        builder.add_node(
            ProvenanceNode(
                ProvenanceNodeType.DATASET,
                "dataset-record.v1",
                record.dataset_id,
                record.content_hash,
                (),
                self._dataset_status(read.status),
                "dataset_manifest_and_normalized_artifact",
                classification,
                (
                    ("source", record.source),
                    ("source_version", record.source_version),
                    ("normalized_content_hash", record.normalized_content_hash),
                    ("instruments", list(record.instruments)),
                    ("timeframe", record.timeframe),
                    ("timestamp_semantics", record.timestamp_semantics),
                    ("calendar_id", record.calendar_id),
                    ("classification", record.classification.value),
                ),
            )
        )
        builder.add_node(
            ProvenanceNode(
                ProvenanceNodeType.DATASET_MANIFEST,
                record.schema_version,
                record.dataset_id,
                record.manifest_hash,
                (),
                self._dataset_status(read.manifest_status),
                "dataset_manifest",
                classification,
            )
        )
        builder.add_relationship(
            ProvenanceRelationship(
                ProvenanceRelationshipType.MANIFEST_DESCRIBES_DATASET,
                _node_id(ProvenanceNodeType.DATASET_MANIFEST, record.dataset_id),
                _node_id(ProvenanceNodeType.DATASET, record.dataset_id),
                self._dataset_status(read.manifest_status),
                "dataset manifest dataset_id reference",
                (record.manifest_hash, record.content_hash),
            )
        )
        admission_identity = f"{record.dataset_id}@{record.quality_report_hash}"
        builder.add_node(
            ProvenanceNode(
                ProvenanceNodeType.DATASET_ADMISSION,
                "dataset-admission-reference.v1",
                admission_identity,
                record.quality_report_hash,
                (),
                self._dataset_status(read.quality_report_status),
                "dataset_quality_report",
                classification,
                (("dataset_id", record.dataset_id),),
            )
        )
        builder.add_relationship(
            ProvenanceRelationship(
                ProvenanceRelationshipType.ADMISSION_EVALUATES_DATASET,
                _node_id(ProvenanceNodeType.DATASET_ADMISSION, admission_identity),
                _node_id(ProvenanceNodeType.DATASET, record.dataset_id),
                self._dataset_status(read.quality_report_status),
                "dataset manifest quality_report_hash reference",
                (record.quality_report_hash, record.content_hash),
            )
        )
        raw_status = self._dataset_status(read.raw_status)
        for raw_hash in record.raw_artifact_hashes:
            builder.add_node(
                ProvenanceNode(
                    ProvenanceNodeType.RAW_ARTIFACT,
                    "raw-artifact-reference.v1",
                    raw_hash,
                    raw_hash,
                    (),
                    raw_status,
                    "dataset_manifest artifact_metadata",
                    classification,
                )
            )
            builder.add_relationship(
                ProvenanceRelationship(
                    ProvenanceRelationshipType.RAW_ARTIFACT_SUPPORTS_DATASET,
                    _node_id(ProvenanceNodeType.RAW_ARTIFACT, raw_hash),
                    _node_id(ProvenanceNodeType.DATASET, record.dataset_id),
                    raw_status,
                    "dataset manifest source_artifact_hashes reference",
                    (raw_hash, record.manifest_hash),
                )
            )
        component_findings = (
            (read.normalized_status, "NORMALIZED_DATASET_UNVERIFIED"),
            (read.raw_status, "RAW_ARTIFACT_UNVERIFIED"),
            (read.quality_report_status, "DATASET_ADMISSION_UNVERIFIED"),
        )
        for status, code in component_findings:
            if status is not DatasetRecordStatus.VERIFIED:
                builder.add_finding(
                    code,
                    self._dataset_status(status),
                    ProvenanceNodeType.DATASET,
                    record.dataset_id,
                )

    def _add_qualification(self, builder: _GraphBuilder, read: QualificationRead) -> None:
        record = read.record
        classification = (
            EvidenceClassification.TEST_FIXTURE
            if record is not None and record.fixture
            else EvidenceClassification.EMPIRICAL
            if record is not None
            and record.empirical_eligibility
            is ResearchDatasetEligibility.EMPIRICALLY_QUALIFIED_DATASET
            else EvidenceClassification.UNKNOWN
        )
        builder.add_node(
            ProvenanceNode(
                ProvenanceNodeType.DATASET_QUALIFICATION,
                "dataset-qualification.v1",
                read.qualification_id,
                read.qualification_id,
                (
                    (
                        ProvenanceTimestamp(
                            "checked_at", record.checked_at, "qualification check time"
                        ),
                    )
                    if record is not None
                    else ()
                ),
                self._dataset_status(read.status),
                "dataset_qualification_registry",
                classification,
                (("qualification", record.canonical()),) if record is not None else (),
            )
        )
        if record is None:
            builder.add_finding(
                read.failure_code or "QUALIFICATION_UNVERIFIED",
                self._dataset_status(read.status),
                ProvenanceNodeType.DATASET_QUALIFICATION,
                read.qualification_id,
            )
            return
        self._bind_qualification_to_dataset(builder, record)

    def _bind_qualification_to_dataset(
        self, builder: _GraphBuilder, record: DatasetQualificationRecord
    ) -> None:
        dataset_id = record.dataset_id
        if _node_id(ProvenanceNodeType.DATASET, dataset_id) not in builder.nodes:
            self._add_unavailable_dataset(builder, dataset_id, IntegrityStatus.MISSING)
        dataset_node = builder.nodes[_node_id(ProvenanceNodeType.DATASET, dataset_id)]
        status = IntegrityStatus.VERIFIED
        if dataset_node.content_hash != record.dataset_content_hash:
            status = IntegrityStatus.HASH_MISMATCH
            builder.add_finding(
                "QUALIFICATION_DATASET_CONTENT_HASH_MISMATCH",
                status,
                ProvenanceNodeType.DATASET_QUALIFICATION,
                record.qualification_id,
            )
        builder.add_relationship(
            ProvenanceRelationship(
                ProvenanceRelationshipType.QUALIFICATION_EVALUATES_DATASET,
                _node_id(ProvenanceNodeType.DATASET_QUALIFICATION, record.qualification_id),
                _node_id(ProvenanceNodeType.DATASET, dataset_id),
                status,
                "qualification dataset_id and content hash references",
                (record.qualification_id, record.dataset_content_hash, record.manifest_hash),
            )
        )

    def _graph_from_builder(
        self,
        builder: _GraphBuilder,
        *,
        query_scope: str,
        consistency_boundary: str,
    ) -> ProvenanceGraph:
        cycles = builder.cycle_nodes()
        if cycles:
            builder.add_finding(
                "PROVENANCE_CYCLE_DETECTED",
                IntegrityStatus.INVALID_SCHEMA,
                ProvenanceNodeType.DATASET,
                cycles[0],
            )
        return ProvenanceGraph(
            query_scope=query_scope,
            query_timestamp=self._now(),
            consistency_boundary=consistency_boundary,
            governance_revision=None,
            nodes=tuple(sorted(builder.nodes.values(), key=lambda item: item.node_id)),
            relationships=tuple(
                sorted(builder.relationships.values(), key=lambda item: item.relationship_id)
            ),
            findings=tuple(
                    sorted(
                        builder.findings.values(),
                        key=lambda item: (item.code, item.stable_identity),
                    )
            ),
            eligibility=None,
            history_truncated=False,
            attachments_truncated=False,
            next_after_revision=None,
        )

    def explain_current_eligibility(
        self, strategy_id: str, strategy_version: str, *, actor_id: str
    ) -> ProvenanceEligibilityExplanation:
        return self.get_strategy_provenance(
            strategy_id, strategy_version, actor_id=actor_id, history_limit=1
        ).eligibility or self._unavailable_explanation(strategy_id, strategy_version)

    def explain_lifecycle_event(
        self,
        strategy_id: str,
        strategy_version: str,
        event_id: str,
        *,
        actor_id: str,
    ) -> HistoricalEventExplanation:
        self._authorize(actor_id)
        snapshot = self._database_reader.read_strategy(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            history_limit=1,
            after_revision=0,
            evidence_limit=100,
            approval_limit=100,
            event_id=event_id,
        )
        event_read = snapshot.events[0] if snapshot.events else None
        if event_read is None:
            return HistoricalEventExplanation(
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                event_id=event_id,
                integrity_status=IntegrityStatus.MISSING,
                event=None,
                policy_reference=None,
                evidence_id=None,
                approval_id=None,
                evidence_revoked_at_event=None,
                evidence_revoked_now=None,
                approval_revoked_at_event=None,
                approval_revoked_now=None,
                historical_reconstruction_complete=False,
                limitations=("lifecycle event is not present in the durable event store",),
                summary="Historical lifecycle event is unavailable.",
                query_timestamp=self._now(),
            )
        if event_read.value is None:
            return HistoricalEventExplanation(
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                event_id=event_id,
                integrity_status=event_read.status,
                event=None,
                policy_reference=None,
                evidence_id=None,
                approval_id=None,
                evidence_revoked_at_event=None,
                evidence_revoked_now=None,
                approval_revoked_at_event=None,
                approval_revoked_now=None,
                historical_reconstruction_complete=False,
                limitations=("lifecycle event schema is invalid",),
                summary="Historical lifecycle event cannot be verified.",
                query_timestamp=self._now(),
            )
        event = event_read.value
        event_status = event_read.status
        if snapshot.artifact is not None and event.artifact_hash != snapshot.artifact.artifact_hash:
            event_status = IntegrityStatus.HASH_MISMATCH
        evidence_revoked_now, evidence_revoked_at_event = self._revocation_state(
            self._verified_revocations(snapshot.evidence_revocations),
            event.evidence_id,
            event.occurred_at,
        )
        approval_revoked_now, approval_revoked_at_event = self._revocation_state(
            self._verified_revocations(snapshot.approval_revocations),
            event.approval_id,
            event.occurred_at,
        )
        limitations = [
            "policy definition content is not persisted in the governance store",
            "historical explanation uses recorded event references and does not "
            "reconstruct all prior state",
        ]
        summary = (
            f"Recorded {event.previous_stage.value}->{event.resulting_stage.value} decision "
            f"at revision {event.resulting_revision}."
        )
        policy = (event.policy_id, event.policy_version)
        return HistoricalEventExplanation(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            event_id=event_id,
            integrity_status=event_status,
            event=event,
            policy_reference=policy,
            evidence_id=event.evidence_id,
            approval_id=event.approval_id,
            evidence_revoked_at_event=evidence_revoked_at_event,
            evidence_revoked_now=evidence_revoked_now,
            approval_revoked_at_event=approval_revoked_at_event,
            approval_revoked_now=approval_revoked_now,
            historical_reconstruction_complete=False,
            limitations=tuple(limitations),
            summary=summary,
            query_timestamp=self._now(),
        )

    def export_provenance_bundle(
        self,
        strategy_id: str,
        strategy_version: str,
        *,
        actor_id: str,
        destination: Path | None = None,
        history_limit: int = 100,
        after_revision: int = 0,
    ) -> ProvenanceExport:
        graph = self.get_strategy_provenance(
            strategy_id,
            strategy_version,
            actor_id=actor_id,
            history_limit=history_limit,
            after_revision=after_revision,
        )
        content = graph.content_payload()
        digest = hashlib.sha256(_canonical_bytes(content)).hexdigest()
        export = ProvenanceExport(content, self._now(), digest)
        if destination is not None:
            self._write_export(destination, export.to_json().encode("utf-8"))
        return export

    def export_graph_bundle(
        self, graph: ProvenanceGraph, *, destination: Path | None = None
    ) -> ProvenanceExport:
        """Export a previously resolved read-only graph without another query."""

        content = graph.content_payload()
        digest = hashlib.sha256(_canonical_bytes(content)).hexdigest()
        export = ProvenanceExport(content, self._now(), digest)
        if destination is not None:
            self._write_export(destination, export.to_json().encode("utf-8"))
        return export

    def _build_graph(
        self,
        snapshot: _DatabaseSnapshot,
        *,
        strategy_id: str,
        strategy_version: str,
        query_scope: str,
    ) -> ProvenanceGraph:
        builder = _GraphBuilder()
        state = snapshot.state
        if state is None:
            state_identity = f"{strategy_id}@{strategy_version}"
            builder.add_node(
                ProvenanceNode(
                    ProvenanceNodeType.STRATEGY_STATE,
                    "strategy-state.v1",
                    state_identity,
                    None,
                    (),
                    snapshot.state_status,
                    "strategy_lifecycle_state",
                    EvidenceClassification.NOT_APPLICABLE,
                )
            )
            builder.add_finding(
                "STRATEGY_STATE_UNAVAILABLE",
                snapshot.state_status,
                ProvenanceNodeType.STRATEGY_STATE,
                state_identity,
            )
        else:
            self._add_state(builder, state, snapshot.state_status)
        artifact_identity = f"{strategy_id}@{strategy_version}"
        if snapshot.artifact is not None:
            artifact_node = ProvenanceNode(
                ProvenanceNodeType.STRATEGY_ARTIFACT,
                snapshot.artifact.schema_version,
                artifact_identity,
                snapshot.artifact.artifact_hash,
                (),
                snapshot.artifact_status,
                "strategy_artifacts",
                EvidenceClassification.NOT_APPLICABLE,
                (("artifact", snapshot.artifact.canonical()),),
            )
            builder.add_node(artifact_node)
            if snapshot.artifact.training_reference:
                self._add_artifact_reference(
                    builder,
                    snapshot.artifact,
                    snapshot.artifact.training_reference.reference_id,
                    "training",
                    expected_dataset_hash=snapshot.artifact.training_reference.dataset_hash,
                    expected_scope=snapshot.artifact.training_reference.scope,
                )
            if snapshot.artifact.validation_reference:
                self._add_artifact_reference(
                    builder,
                    snapshot.artifact,
                    snapshot.artifact.validation_reference.reference_id,
                    "validation",
                    expected_dataset_hash=snapshot.artifact.validation_reference.dataset_hash,
                    expected_scope=snapshot.artifact.validation_reference.scope,
                )
            if snapshot.artifact.oos_reference:
                self._add_artifact_reference(
                    builder,
                    snapshot.artifact,
                    snapshot.artifact.oos_reference.reference_id,
                    "locked_oos",
                    expected_dataset_hash=snapshot.artifact.oos_reference.dataset_hash,
                    expected_scope=snapshot.artifact.oos_reference.scope,
                )
        else:
            builder.add_node(
                ProvenanceNode(
                    ProvenanceNodeType.STRATEGY_ARTIFACT,
                    "strategy-artifact.v1",
                    artifact_identity,
                    snapshot.artifact_hash,
                    (),
                    snapshot.artifact_status,
                    "strategy_artifacts",
                    EvidenceClassification.NOT_APPLICABLE,
                )
            )
            builder.add_finding(
                "STRATEGY_ARTIFACT_UNVERIFIED",
                snapshot.artifact_status,
                ProvenanceNodeType.STRATEGY_ARTIFACT,
                artifact_identity,
            )
        if state is not None:
            builder.add_relationship(
                ProvenanceRelationship(
                    ProvenanceRelationshipType.STATE_PROJECTS_ARTIFACT,
                    _node_id(ProvenanceNodeType.STRATEGY_STATE, artifact_identity),
                    _node_id(ProvenanceNodeType.STRATEGY_ARTIFACT, artifact_identity),
                    IntegrityStatus.VERIFIED
                    if snapshot.artifact is not None
                    else snapshot.artifact_status,
                    "strategy_lifecycle_state",
                )
            )
        experiment_cache: dict[str, _ExperimentRead] = {}
        evidence_records: list[tuple[_StoredEvidenceRead, bool]] = []
        for record in snapshot.evidence:
            revoked = any(
                item.value is not None and item.value.target_id == record.evidence_id
                for item in snapshot.evidence_revocations
            )
            evidence_records.append((record, revoked))
            self._add_evidence(builder, record, revoked, snapshot.artifact, experiment_cache)
        for approval_record in snapshot.approvals:
            approval_revoked = any(
                item.value is not None and item.value.target_id == approval_record.approval_id
                for item in snapshot.approval_revocations
            )
            self._add_approval(builder, approval_record, approval_revoked, snapshot.artifact)
        for evidence_revocation in snapshot.evidence_revocations:
            self._add_revocation(builder, evidence_revocation)
        for approval_revocation in snapshot.approval_revocations:
            self._add_revocation(builder, approval_revocation)
        for event_record in snapshot.events:
            self._add_event(builder, event_record, snapshot.artifact)
        if snapshot.history_truncated:
            builder.add_finding(
                "HISTORY_TRUNCATED",
                IntegrityStatus.UNSUPPORTED,
                ProvenanceNodeType.LIFECYCLE_EVENT,
                artifact_identity,
            )
        if snapshot.attachments_truncated:
            builder.add_finding(
                "ATTACHMENTS_TRUNCATED",
                IntegrityStatus.UNSUPPORTED,
                ProvenanceNodeType.EVIDENCE,
                artifact_identity,
            )
        for read in experiment_cache.values():
            if read.facts is not None and snapshot.artifact is not None:
                if (
                    read.facts.strategy_id == snapshot.artifact.strategy_id
                    and read.facts.strategy_version == snapshot.artifact.strategy_version
                ):
                    builder.add_relationship(
                        ProvenanceRelationship(
                            ProvenanceRelationshipType.EXPERIMENT_EVALUATES_ARTIFACT,
                            _node_id(ProvenanceNodeType.EXPERIMENT, read.experiment_id),
                            _node_id(ProvenanceNodeType.STRATEGY_ARTIFACT, artifact_identity),
                            IntegrityStatus.VERIFIED,
                            "experiment_registry plus evidence attachment",
                            (read.experiment_id,),
                        )
                    )
                else:
                    builder.add_finding(
                        "EXPERIMENT_STRATEGY_IDENTITY_MISMATCH",
                        IntegrityStatus.HASH_MISMATCH,
                        ProvenanceNodeType.EXPERIMENT,
                        read.experiment_id,
                    )
        eligibility = self._build_eligibility(snapshot, evidence_records, experiment_cache)
        policy_refs = self._policy_references(snapshot)
        for policy_id, policy_version in policy_refs:
            identity = f"{policy_id}@{policy_version}"
            builder.add_node(
                ProvenanceNode(
                    ProvenanceNodeType.POLICY,
                    "policy-reference.v1",
                    identity,
                    None,
                    (),
                    IntegrityStatus.UNSUPPORTED,
                    "policy_identity_record",
                    EvidenceClassification.NOT_APPLICABLE,
                    (("policy_id", policy_id), ("policy_version", policy_version)),
                )
            )
            builder.add_finding(
                "POLICY_DEFINITION_UNAVAILABLE",
                IntegrityStatus.UNSUPPORTED,
                ProvenanceNodeType.POLICY,
                identity,
            )
            policy_node_id = _node_id(ProvenanceNodeType.POLICY, identity)
            for approval_record in snapshot.approvals:
                if approval_record.value is not None and (
                    approval_record.value.policy_id == policy_id
                    and approval_record.value.policy_version == policy_version
                ):
                    builder.add_relationship(
                        ProvenanceRelationship(
                            ProvenanceRelationshipType.APPLIES_POLICY,
                            _node_id(ProvenanceNodeType.APPROVAL, approval_record.approval_id),
                            policy_node_id,
                            IntegrityStatus.UNSUPPORTED,
                            "strategy_approvals policy identity",
                            (approval_record.approval_id,),
                        )
                    )
            for event_record in snapshot.events:
                if event_record.value is not None and (
                    event_record.value.policy_id == policy_id
                    and event_record.value.policy_version == policy_version
                ):
                    builder.add_relationship(
                        ProvenanceRelationship(
                            ProvenanceRelationshipType.APPLIES_POLICY,
                            _node_id(ProvenanceNodeType.LIFECYCLE_EVENT, event_record.event_id),
                            policy_node_id,
                            IntegrityStatus.UNSUPPORTED,
                            "strategy_lifecycle_events policy identity",
                            (event_record.event_id,),
                        )
                    )
        cycles = builder.cycle_nodes()
        if cycles:
            builder.add_finding(
                "PROVENANCE_CYCLE_DETECTED",
                IntegrityStatus.INVALID_SCHEMA,
                ProvenanceNodeType.STRATEGY_ARTIFACT,
                artifact_identity,
            )
        query_timestamp = snapshot.query_timestamp
        return ProvenanceGraph(
            query_scope=query_scope,
            query_timestamp=query_timestamp,
            consistency_boundary=self._CONSISTENCY,
            governance_revision=state.revision if state else None,
            nodes=tuple(sorted(builder.nodes.values(), key=lambda item: item.node_id)),
            relationships=tuple(
                sorted(builder.relationships.values(), key=lambda item: item.relationship_id)
            ),
            findings=tuple(
                sorted(
                    builder.findings.values(), key=lambda item: (item.code, item.stable_identity)
                )
            ),
            eligibility=eligibility,
            history_truncated=snapshot.history_truncated,
            attachments_truncated=snapshot.attachments_truncated,
            next_after_revision=snapshot.next_after_revision,
        )

    @staticmethod
    def _add_state(builder: _GraphBuilder, state: StrategyState, status: IntegrityStatus) -> None:
        identity = f"{state.strategy_id}@{state.strategy_version}"
        builder.add_node(
            ProvenanceNode(
                ProvenanceNodeType.STRATEGY_STATE,
                "strategy-state.v1",
                identity,
                None,
                (ProvenanceTimestamp("updated_at", state.updated_at, "projection update time"),),
                status,
                "strategy_lifecycle_state",
                EvidenceClassification.NOT_APPLICABLE,
                (
                    ("stage", state.stage.value),
                    ("health", state.health.value),
                    ("revision", state.revision),
                    ("disabled", state.disabled),
                    ("disable_reason", state.disable_reason),
                ),
            )
        )

    def _add_artifact_reference(
        self,
        builder: _GraphBuilder,
        artifact: StrategyArtifact,
        reference_id: str,
        label: str,
        *,
        expected_dataset_hash: str,
        expected_scope: ResearchScope,
    ) -> None:
        read = self._experiment_reader.read(reference_id)
        self._add_experiment(builder, read)
        status = read.status
        if read.facts is not None and (
            read.facts.strategy_id != artifact.strategy_id
            or read.facts.strategy_version != artifact.strategy_version
            or read.facts.dataset_hash != expected_dataset_hash
            or read.facts.scope is not expected_scope
        ):
            status = IntegrityStatus.HASH_MISMATCH
            builder.add_finding(
                "ARTIFACT_RESEARCH_REFERENCE_MISMATCH",
                status,
                ProvenanceNodeType.EXPERIMENT,
                reference_id,
            )
        builder.add_relationship(
            ProvenanceRelationship(
                ProvenanceRelationshipType.ARTIFACT_REFERENCES_EXPERIMENT,
                _node_id(
                    ProvenanceNodeType.STRATEGY_ARTIFACT,
                    f"{artifact.strategy_id}@{artifact.strategy_version}",
                ),
                _node_id(ProvenanceNodeType.EXPERIMENT, reference_id),
                status,
                "strategy_artifact research reference",
                (reference_id,),
                label,
            )
        )
        if status is not IntegrityStatus.VERIFIED:
            builder.add_finding(
                "ARTIFACT_RESEARCH_REFERENCE_UNAVAILABLE",
                status,
                ProvenanceNodeType.EXPERIMENT,
                reference_id,
            )

    def _add_experiment(self, builder: _GraphBuilder, read: _ExperimentRead) -> None:
        identity = read.experiment_id
        if read.facts is None:
            builder.add_node(
                ProvenanceNode(
                    ProvenanceNodeType.EXPERIMENT,
                    "phase9-experiment.v1",
                    identity,
                    None,
                    (),
                    read.status,
                    "experiment_registry",
                    EvidenceClassification.UNKNOWN,
                )
            )
            if read.failure_code:
                builder.add_finding(
                    read.failure_code,
                    read.status,
                    ProvenanceNodeType.EXPERIMENT,
                    identity,
                )
            return
        facts = read.facts
        builder.add_node(
            ProvenanceNode(
                ProvenanceNodeType.EXPERIMENT,
                facts.schema_version,
                identity,
                facts.experiment_hash,
                (),
                read.status,
                "experiment_registry",
                EvidenceClassification.UNKNOWN,
                (
                    ("dataset_version", facts.dataset_version),
                    ("dataset_hash", facts.dataset_hash),
                    ("scope", facts.scope.value),
                    ("strategy_id", facts.strategy_id),
                    ("strategy_version", facts.strategy_version),
                    ("selection_eligible", facts.selection_eligible),
                    (
                        "input_binding",
                        facts.input_binding.canonical() if facts.input_binding else None,
                    ),
                    ("canonical_experiment", facts.canonical_payload),
                ),
            )
        )
        result_identity = f"{facts.experiment_id}@{facts.result_hash}"
        builder.add_node(
            ProvenanceNode(
                ProvenanceNodeType.EXPERIMENT_RESULT,
                "phase9-result.v1",
                result_identity,
                facts.result_hash,
                (),
                IntegrityStatus.VERIFIED,
                "experiment_registry",
                EvidenceClassification.UNKNOWN,
                (
                    ("decision", facts.result_decision.value),
                    ("locked_oos_touched", facts.result_locked_oos_touched),
                    ("selection_influenced_by_locked_oos", facts.result_contaminated),
                    ("metrics", list(facts.result_metrics)),
                    ("notes", list(facts.result_notes)),
                ),
            )
        )
        builder.add_relationship(
            ProvenanceRelationship(
                ProvenanceRelationshipType.RESULT_BELONGS_TO_EXPERIMENT,
                _node_id(ProvenanceNodeType.EXPERIMENT_RESULT, result_identity),
                _node_id(ProvenanceNodeType.EXPERIMENT, identity),
                IntegrityStatus.VERIFIED,
                "experiment_registry",
                (facts.result_hash,),
            )
        )
        if facts.input_binding is None:
            dataset_identity = facts.dataset_hash
            builder.add_node(
                ProvenanceNode(
                    ProvenanceNodeType.DATASET_MANIFEST,
                    "dataset-reference.v1",
                    dataset_identity,
                    facts.dataset_hash,
                    (),
                    IntegrityStatus.UNSUPPORTED,
                    "dataset_manifest_registry",
                    EvidenceClassification.UNKNOWN,
                    (("dataset_version", facts.dataset_version),),
                )
            )
            builder.add_finding(
                "DATASET_MANIFEST_REGISTRY_UNAVAILABLE",
                IntegrityStatus.UNSUPPORTED,
                ProvenanceNodeType.DATASET_MANIFEST,
                dataset_identity,
            )
            builder.add_finding(
                "EXPERIMENT_INPUT_BINDING_MISSING",
                IntegrityStatus.UNSUPPORTED,
                ProvenanceNodeType.EXPERIMENT,
                identity,
            )
            builder.add_relationship(
                ProvenanceRelationship(
                    ProvenanceRelationshipType.EXPERIMENT_USES_DATASET,
                    _node_id(ProvenanceNodeType.EXPERIMENT, identity),
                    _node_id(ProvenanceNodeType.DATASET_MANIFEST, dataset_identity),
                    IntegrityStatus.UNSUPPORTED,
                    "legacy experiment dataset_hash reference",
                    (facts.dataset_hash,),
                )
            )
        else:
            self._add_bound_experiment_inputs(builder, identity, facts)
        if facts.candidate_id is not None:
            candidate_identity = facts.candidate_id
            builder.add_node(
                ProvenanceNode(
                    ProvenanceNodeType.CANDIDATE,
                    "strategy-candidate-reference.v1",
                    candidate_identity,
                    None,
                    (),
                    IntegrityStatus.MISSING,
                    "candidate_registry",
                    EvidenceClassification.UNKNOWN,
                )
            )
            builder.add_finding(
                "CANDIDATE_REGISTRY_UNAVAILABLE",
                IntegrityStatus.MISSING,
                ProvenanceNodeType.CANDIDATE,
                candidate_identity,
            )
            builder.add_relationship(
                ProvenanceRelationship(
                    ProvenanceRelationshipType.EXPERIMENT_REFERENCES_CANDIDATE,
                    _node_id(ProvenanceNodeType.EXPERIMENT, identity),
                    _node_id(ProvenanceNodeType.CANDIDATE, candidate_identity),
                    IntegrityStatus.MISSING,
                    "experiment_registry",
                    (identity,),
                )
            )

    def _add_bound_experiment_inputs(
        self, builder: _GraphBuilder, experiment_id: str, facts: _ExperimentFacts
    ) -> None:
        binding = facts.input_binding
        if binding is None:
            return
        dataset_read = (
            self._dataset_reader.read(binding.dataset_id)
            if self._dataset_reader is not None
            else None
        )
        if dataset_read is None:
            self._add_unavailable_dataset(builder, binding.dataset_id, IntegrityStatus.UNSUPPORTED)
        else:
            self._add_dataset_record(builder, dataset_read)
        qualification_read = (
            self._qualification_registry.read(binding.qualification_id)
            if self._qualification_registry is not None
            else None
        )
        if qualification_read is None:
            self._add_unavailable_qualification(builder, binding.qualification_id)
        else:
            self._add_qualification(builder, qualification_read)
        verification = self._verify_input_binding(facts)
        dataset_node_id = _node_id(ProvenanceNodeType.DATASET, binding.dataset_id)
        if dataset_node_id not in builder.nodes:
            self._add_unavailable_dataset(builder, binding.dataset_id, verification.dataset_status)
        builder.add_relationship(
            ProvenanceRelationship(
                ProvenanceRelationshipType.EXPERIMENT_USES_DATASET,
                _node_id(ProvenanceNodeType.EXPERIMENT, experiment_id),
                dataset_node_id,
                (
                    IntegrityStatus.HASH_MISMATCH
                    if any(
                        code
                        in {
                            "DATASET_CONTENT_HASH_MISMATCH",
                            "DATASET_MANIFEST_HASH_MISMATCH",
                        }
                        for code in verification.reasons
                    )
                    else verification.dataset_status
                ),
                "experiment input binding dataset identity and content hash",
                (
                    binding.dataset_id,
                    binding.dataset_content_hash,
                    binding.manifest_hash,
                ),
            )
        )
        qualification_node_id = _node_id(
            ProvenanceNodeType.DATASET_QUALIFICATION, binding.qualification_id
        )
        builder.add_relationship(
            ProvenanceRelationship(
                ProvenanceRelationshipType.EXPERIMENT_USES_QUALIFICATION,
                _node_id(ProvenanceNodeType.EXPERIMENT, experiment_id),
                qualification_node_id,
                (
                    IntegrityStatus.HASH_MISMATCH
                    if any(
                        code
                        in {"QUALIFICATION_DATASET_MISMATCH", "QUALIFICATION_POLICY_MISMATCH"}
                        for code in verification.reasons
                    )
                    else verification.qualification_status
                ),
                "experiment input binding qualification identity and policy",
                (
                    binding.qualification_id,
                    binding.qualification_policy_id,
                    binding.qualification_policy_version,
                ),
            )
        )
        slice_identity = binding.data_slice_identity
        builder.add_node(
            ProvenanceNode(
                ProvenanceNodeType.DATA_SLICE,
                "experiment-data-slice.v1",
                slice_identity,
                None,
                (),
                verification.slice_status,
                "experiment input binding and normalized dataset",
                EvidenceClassification.TEST_FIXTURE
                if binding.fixture
                else EvidenceClassification.EMPIRICAL,
                (
                    ("dataset_id", binding.dataset_id),
                    (
                        "evaluation_period",
                        {
                            "start": binding.evaluation_period.start.isoformat(),
                            "end": binding.evaluation_period.end.isoformat(),
                        },
                    ),
                    (
                        "warmup_period",
                        {
                            "start": binding.warmup_period.start.isoformat(),
                            "end": binding.warmup_period.end.isoformat(),
                        }
                        if binding.warmup_period
                        else None,
                    ),
                    ("instrument_scope", list(binding.instrument_scope)),
                    ("feature_input_lineage", list(binding.feature_input_lineage)),
                ),
            )
        )
        builder.add_relationship(
            ProvenanceRelationship(
                ProvenanceRelationshipType.DATA_SLICE_DERIVED_FROM_DATASET,
                _node_id(ProvenanceNodeType.DATA_SLICE, slice_identity),
                dataset_node_id,
                verification.slice_status,
                "verified normalized dataset slice derivation",
                (binding.data_slice_identity, binding.dataset_content_hash),
            )
        )
        for reason in verification.reasons:
            builder.add_finding(
                reason,
                verification.integrity_status,
                ProvenanceNodeType.EXPERIMENT,
                experiment_id,
            )

    def _add_evidence(
        self,
        builder: _GraphBuilder,
        record: _StoredEvidenceRead,
        revoked: bool,
        artifact: StrategyArtifact | None,
        experiment_cache: dict[str, _ExperimentRead],
    ) -> None:
        identity = record.evidence_id
        value = record.value
        classification = EvidenceClassification.UNKNOWN
        attributes: tuple[tuple[str, object], ...] = ()
        timestamps: tuple[ProvenanceTimestamp, ...] = ()
        if value is not None:
            classification = (
                EvidenceClassification.TEST_FIXTURE
                if value.deterministic_test_fixture
                else EvidenceClassification.EMPIRICAL
                if value.empirical_eligible
                else EvidenceClassification.UNKNOWN
            )
            attributes = (("evidence", value.canonical()), ("revoked", revoked))
        builder.add_node(
            ProvenanceNode(
                ProvenanceNodeType.EVIDENCE,
                value.schema_version if value is not None else "strategy-evidence.v1",
                identity,
                record.evidence_hash,
                (
                    (
                        ProvenanceTimestamp(
                            "attached_at", record.attached_at, "evidence attachment time"
                        ),
                    )
                    if record.attached_at is not None
                    else timestamps
                ),
                record.status,
                "strategy_evidence",
                classification,
                attributes,
            )
        )
        if record.status is not IntegrityStatus.VERIFIED or value is None:
            builder.add_finding(
                "EVIDENCE_UNVERIFIED",
                record.status,
                ProvenanceNodeType.EVIDENCE,
                identity,
            )
            return
        if artifact is not None:
            artifact_identity = f"{artifact.strategy_id}@{artifact.strategy_version}"
            relation_status = (
                IntegrityStatus.VERIFIED
                if value.artifact_hash == artifact.artifact_hash
                else IntegrityStatus.HASH_MISMATCH
            )
            builder.add_relationship(
                ProvenanceRelationship(
                    ProvenanceRelationshipType.EVIDENCE_SUPPORTS_ARTIFACT,
                    _node_id(ProvenanceNodeType.EVIDENCE, identity),
                    _node_id(ProvenanceNodeType.STRATEGY_ARTIFACT, artifact_identity),
                    relation_status,
                    "strategy_evidence",
                    (value.artifact_hash,),
                )
            )
            if relation_status is IntegrityStatus.HASH_MISMATCH:
                builder.add_finding(
                    "EVIDENCE_ARTIFACT_HASH_MISMATCH",
                    relation_status,
                    ProvenanceNodeType.EVIDENCE,
                    identity,
                )
        experiment_read = experiment_cache.setdefault(
            value.experiment_id, self._experiment_reader.read(value.experiment_id)
        )
        self._add_experiment(builder, experiment_read)
        relation_status = experiment_read.status
        if experiment_read.facts is not None and (
            experiment_read.facts.result_hash != value.result_hash
            or (
                artifact is not None
                and (
                    experiment_read.facts.strategy_id != artifact.strategy_id
                    or experiment_read.facts.strategy_version != artifact.strategy_version
                )
            )
            or experiment_read.facts.dataset_hash not in value.dataset_hashes
        ):
            relation_status = IntegrityStatus.HASH_MISMATCH
            builder.add_finding(
                "EVIDENCE_RESULT_HASH_MISMATCH"
                if experiment_read.facts.result_hash != value.result_hash
                else "EVIDENCE_REFERENCE_MISMATCH",
                relation_status,
                ProvenanceNodeType.EVIDENCE,
                identity,
            )
        builder.add_relationship(
            ProvenanceRelationship(
                ProvenanceRelationshipType.EVIDENCE_REFERENCES_EXPERIMENT,
                _node_id(ProvenanceNodeType.EVIDENCE, identity),
                _node_id(ProvenanceNodeType.EXPERIMENT, value.experiment_id),
                relation_status,
                "strategy_evidence",
                (value.experiment_id, value.result_hash),
            )
        )
        for dataset_hash in value.dataset_hashes:
            builder.add_node(
                ProvenanceNode(
                    ProvenanceNodeType.DATASET_MANIFEST,
                    "dataset-reference.v1",
                    dataset_hash,
                    dataset_hash,
                    (),
                    IntegrityStatus.UNSUPPORTED,
                    "dataset_manifest_registry",
                    classification,
                )
            )
            builder.add_relationship(
                ProvenanceRelationship(
                    ProvenanceRelationshipType.EVIDENCE_REFERENCES_DATASET,
                    _node_id(ProvenanceNodeType.EVIDENCE, identity),
                    _node_id(ProvenanceNodeType.DATASET_MANIFEST, dataset_hash),
                    IntegrityStatus.VERIFIED,
                    "strategy_evidence",
                    (dataset_hash,),
                )
            )

    @staticmethod
    def _add_approval(
        builder: _GraphBuilder,
        record: _StoredApprovalRead,
        revoked: bool,
        artifact: StrategyArtifact | None,
    ) -> None:
        identity = record.approval_id
        value = record.value
        attributes: tuple[tuple[str, object], ...] = (("revoked", revoked),)
        timestamps: tuple[ProvenanceTimestamp, ...] = ()
        if value is not None:
            attributes = (("approval", value.canonical()), ("revoked", revoked))
            if record.approved_at is not None:
                timestamps = (
                    ProvenanceTimestamp(
                        "approved_at", record.approved_at, "approval decision time"
                    ),
                )
        builder.add_node(
            ProvenanceNode(
                ProvenanceNodeType.APPROVAL,
                value.schema_version if value is not None else "strategy-approval.v1",
                identity,
                record.approval_hash,
                timestamps,
                record.status,
                "strategy_approvals",
                EvidenceClassification.NOT_APPLICABLE,
                attributes,
            )
        )
        if value is None:
            builder.add_finding(
                "APPROVAL_UNVERIFIED",
                record.status,
                ProvenanceNodeType.APPROVAL,
                identity,
            )
            return
        if artifact is not None:
            artifact_identity = f"{artifact.strategy_id}@{artifact.strategy_version}"
            builder.add_relationship(
                ProvenanceRelationship(
                    ProvenanceRelationshipType.APPROVAL_AUTHORIZES_TRANSITION,
                    _node_id(ProvenanceNodeType.APPROVAL, identity),
                    _node_id(ProvenanceNodeType.STRATEGY_ARTIFACT, artifact_identity),
                    IntegrityStatus.VERIFIED
                    if value.artifact_hash == artifact.artifact_hash
                    else IntegrityStatus.HASH_MISMATCH,
                    "strategy_approvals",
                    (value.artifact_hash, value.evidence_id),
                    value.target_stage.value,
                )
            )
        builder.add_relationship(
            ProvenanceRelationship(
                ProvenanceRelationshipType.APPROVAL_BINDS_EVIDENCE,
                _node_id(ProvenanceNodeType.APPROVAL, identity),
                _node_id(ProvenanceNodeType.EVIDENCE, value.evidence_id),
                IntegrityStatus.VERIFIED,
                "strategy_approvals",
                (value.evidence_id,),
            )
        )

    @staticmethod
    def _add_revocation(builder: _GraphBuilder, read: _StoredRevocationRead) -> None:
        identity = read.revocation_id
        record = read.value
        if record is None:
            builder.add_node(
                ProvenanceNode(
                    ProvenanceNodeType.REVOCATION,
                    "strategy-revocation.v1",
                    identity,
                    None,
                    (),
                    read.status,
                    f"strategy_{read.target_type}_revocations",
                    EvidenceClassification.NOT_APPLICABLE,
                )
            )
            builder.add_finding(
                "REVOCATION_UNVERIFIED",
                read.status,
                ProvenanceNodeType.REVOCATION,
                identity,
            )
            return
        target_type = (
            ProvenanceNodeType.EVIDENCE
            if record.target_type == "evidence"
            else ProvenanceNodeType.APPROVAL
        )
        builder.add_node(
            ProvenanceNode(
                ProvenanceNodeType.REVOCATION,
                "strategy-revocation.v1",
                identity,
                None,
                (ProvenanceTimestamp("revoked_at", record.revoked_at, "revocation time"),),
                IntegrityStatus.VERIFIED,
                f"strategy_{record.target_type}_revocations",
                EvidenceClassification.NOT_APPLICABLE,
                (
                    ("target_id", record.target_id),
                    ("target_type", record.target_type),
                    ("actor_id", record.actor_id),
                    ("reason", record.reason),
                ),
            )
        )
        relationship_type = (
            ProvenanceRelationshipType.REVOCATION_INVALIDATES_EVIDENCE
            if record.target_type == "evidence"
            else ProvenanceRelationshipType.REVOCATION_INVALIDATES_APPROVAL
        )
        builder.add_relationship(
            ProvenanceRelationship(
                relationship_type,
                _node_id(ProvenanceNodeType.REVOCATION, identity),
                _node_id(target_type, record.target_id),
                IntegrityStatus.VERIFIED,
                f"strategy_{record.target_type}_revocations",
                (record.target_id,),
            )
        )

    @staticmethod
    def _add_event(
        builder: _GraphBuilder,
        record: _StoredEventRead,
        artifact: StrategyArtifact | None,
    ) -> None:
        event_id = record.event_id
        value = record.value
        if value is None:
            builder.add_node(
                ProvenanceNode(
                    ProvenanceNodeType.LIFECYCLE_EVENT,
                    "strategy-lifecycle-event.v1",
                    event_id,
                    None,
                    (),
                    record.status,
                    "strategy_lifecycle_events",
                    EvidenceClassification.NOT_APPLICABLE,
                )
            )
            builder.add_finding(
                "LIFECYCLE_EVENT_UNVERIFIED",
                record.status,
                ProvenanceNodeType.LIFECYCLE_EVENT,
                event_id,
            )
            return
        attributes = (
            ("event_type", value.event_type),
            ("previous_stage", value.previous_stage.value),
            ("resulting_stage", value.resulting_stage.value),
            ("expected_revision", value.expected_revision),
            ("resulting_revision", value.resulting_revision),
            ("actor_id", value.actor_id),
            ("policy_id", value.policy_id),
            ("policy_version", value.policy_version),
            ("reason_code", value.reason_code),
            ("reason", value.reason),
            ("evidence_id", value.evidence_id),
            ("approval_id", value.approval_id),
        )
        builder.add_node(
            ProvenanceNode(
                ProvenanceNodeType.LIFECYCLE_EVENT,
                "strategy-lifecycle-event.v1",
                event_id,
                None,
                (ProvenanceTimestamp("occurred_at", value.occurred_at, "event occurrence time"),),
                record.status,
                "strategy_lifecycle_events",
                EvidenceClassification.NOT_APPLICABLE,
                attributes,
            )
        )
        if artifact is not None:
            artifact_identity = f"{artifact.strategy_id}@{artifact.strategy_version}"
            builder.add_relationship(
                ProvenanceRelationship(
                    ProvenanceRelationshipType.EVENT_APPLIES_TO_ARTIFACT,
                    _node_id(ProvenanceNodeType.LIFECYCLE_EVENT, event_id),
                    _node_id(ProvenanceNodeType.STRATEGY_ARTIFACT, artifact_identity),
                    IntegrityStatus.VERIFIED
                    if value.artifact_hash == artifact.artifact_hash
                    else IntegrityStatus.HASH_MISMATCH,
                    "strategy_lifecycle_events",
                    (value.artifact_hash,),
                )
            )
        if value.evidence_id:
            builder.add_relationship(
                ProvenanceRelationship(
                    ProvenanceRelationshipType.EVENT_REFERENCES_EVIDENCE,
                    _node_id(ProvenanceNodeType.LIFECYCLE_EVENT, event_id),
                    _node_id(ProvenanceNodeType.EVIDENCE, value.evidence_id),
                    IntegrityStatus.VERIFIED,
                    "strategy_lifecycle_events",
                    (value.evidence_id,),
                )
            )
        if value.approval_id:
            builder.add_relationship(
                ProvenanceRelationship(
                    ProvenanceRelationshipType.EVENT_REFERENCES_APPROVAL,
                    _node_id(ProvenanceNodeType.LIFECYCLE_EVENT, event_id),
                    _node_id(ProvenanceNodeType.APPROVAL, value.approval_id),
                    IntegrityStatus.VERIFIED,
                    "strategy_lifecycle_events",
                    (value.approval_id,),
                )
            )

    def _build_eligibility(
        self,
        snapshot: _DatabaseSnapshot,
        evidence_records: Sequence[tuple[_StoredEvidenceRead, bool]],
        experiment_cache: Mapping[str, _ExperimentRead],
    ) -> ProvenanceEligibilityExplanation:
        state = snapshot.state
        artifact_hash = (
            snapshot.artifact.artifact_hash if snapshot.artifact else snapshot.artifact_hash
        )
        evidence_summaries = tuple(
            EvidenceEligibilitySummary(
                item.evidence_id,
                item.status,
                item.value.empirical_eligible if item.value else None,
                item.value.deterministic_test_fixture if item.value else None,
                item.value.oos_status.value if item.value else None,
                item.value.validation_verdict.value
                if item.value and item.value.validation_verdict
                else None,
                revoked,
            )
            for item, revoked in evidence_records
        )
        revocation_ids = {
            item.value.target_id for item in snapshot.approval_revocations if item.value is not None
        }
        approval_summaries = tuple(
            ApprovalEligibilitySummary(
                item.approval_id,
                item.value.target_stage.value if item.value else None,
                item.status,
                item.approval_id in revocation_ids,
            )
            for item in snapshot.approvals
        )
        active = tuple(item for item in approval_summaries if not item.revoked)
        revoked = tuple(item for item in approval_summaries if item.revoked)
        policy_refs = self._policy_references(snapshot)
        input_verification: _InputVerification | None = None
        binding_status = "unknown"
        dataset_status: IntegrityStatus | None = None
        dataset_content_hash: str | None = None
        dataset_manifest_hash: str | None = None
        qualification_id: str | None = None
        qualification_status: IntegrityStatus | None = None
        qualification_verdict: str | None = None
        empirical_eligibility: str | None = None
        oos_data_status: str | None = None
        reasons: tuple[str, ...]
        if (
            state is None
            or snapshot.artifact is None
            or snapshot.artifact_status is not IntegrityStatus.VERIFIED
        ):
            decision = EvaluationDecision.REJECT
            reasons = ("ARTIFACT_INTEGRITY_UNVERIFIED",)
        else:
            latest_evidence = evidence_records[0] if evidence_records else None
            latest_value = latest_evidence[0].value if latest_evidence else None
            evidence_revoked = latest_evidence[1] if latest_evidence else False
            cached_experiment = (
                experiment_cache.get(latest_value.experiment_id)
                if latest_value is not None
                else None
            )
            experiment_verified = False
            if cached_experiment is not None and cached_experiment.facts is not None:
                experiment_verified = (
                    cached_experiment.status is IntegrityStatus.VERIFIED
                    and cached_experiment.facts.result_hash == latest_value.result_hash
                    if latest_value is not None
                    else False
                )
                if experiment_verified and cached_experiment.facts.input_binding is not None:
                    input_verification = self._verify_input_binding(cached_experiment.facts)
                    binding = cached_experiment.facts.input_binding
                    binding_status = input_verification.binding_status
                    dataset_status = input_verification.dataset_status
                    dataset_content_hash = binding.dataset_content_hash
                    dataset_manifest_hash = binding.manifest_hash
                    qualification_id = binding.qualification_id
                    qualification_status = input_verification.qualification_status
                    qualification_verdict = input_verification.qualification_verdict
                    empirical_eligibility = input_verification.empirical_eligibility
                    oos_data_status = (
                        "CONTAMINATED"
                        if cached_experiment.facts.result_contaminated
                        else "PRISTINE"
                    )
                elif experiment_verified:
                    binding_status = "LEGACY_UNBOUND"
                    oos_data_status = (
                        "CONTAMINATED"
                        if cached_experiment.facts.result_contaminated
                        else "PRISTINE"
                    )
            if latest_evidence is not None and (
                latest_evidence[0].status is not IntegrityStatus.VERIFIED or not experiment_verified
            ):
                decision = EvaluationDecision.REJECT
                reasons = ("EVIDENCE_INTEGRITY_UNVERIFIED",)
            else:
                latest_approval = next(
                    (
                        item
                        for item in snapshot.approvals
                        if latest_value is not None
                        and item.value is not None
                        and item.value.evidence_id == latest_value.evidence_id
                    ),
                    None,
                )
                approval_revoked = latest_approval is not None and any(
                    item.value is not None and item.value.target_id == latest_approval.approval_id
                    for item in snapshot.approval_revocations
                )
                evaluation = evaluate_current_eligibility(
                    state=state,
                    artifact=snapshot.artifact,
                    evidence=latest_value,
                    approval=latest_approval.value if latest_approval else None,
                    evidence_revoked=evidence_revoked,
                    approval_revoked=approval_revoked,
                )
                decision = evaluation.decision
                reasons_list = list(evaluation.reason_codes)
                if input_verification is not None and input_verification.reasons:
                    reasons_list.extend(input_verification.reasons)
                    hard_input_reasons = {
                        "DATASET_CONTENT_HASH_MISMATCH",
                        "DATASET_MANIFEST_HASH_MISMATCH",
                        "QUALIFICATION_DATASET_MISMATCH",
                        "QUALIFICATION_POLICY_MISMATCH",
                        "DATASET_QUALIFICATION_REJECTED",
                        "EMPIRICAL_ELIGIBILITY_BLOCKED",
                        "FIXTURE_CLASSIFICATION_MISMATCH",
                        "OOS_DATA_CONTAMINATED",
                    }
                    decision = (
                        EvaluationDecision.REJECT
                        if input_verification.integrity_status
                        in {IntegrityStatus.HASH_MISMATCH, IntegrityStatus.INVALID_SCHEMA}
                        or hard_input_reasons.intersection(input_verification.reasons)
                        else EvaluationDecision.HOLD
                    )
                reasons = tuple(dict.fromkeys(reasons_list))
        review_requirements = tuple(
            {
                "MISSING_EVIDENCE": "attach verified validation evidence",
                "EVIDENCE_INTEGRITY_UNVERIFIED": "repair the referenced immutable evidence record",
                "APPROVAL_REQUIRED": "obtain independent approval for the current stage",
                "OOS_STATUS_REQUIRED": "record a pristine locked OOS result",
                "VALIDATION_NOT_PASSED": "complete a passing validation review",
                "STRATEGY_DISABLED": "governed review must clear operational disablement",
                "ARTIFACT_INTEGRITY_UNVERIFIED": (
                    "restore or re-register the exact artifact version"
                ),
                "DATASET_RECORD_MISSING": "restore the referenced immutable dataset record",
                "DATASET_RECORD_INVALID": "repair the referenced dataset manifest or artifacts",
                "DATASET_RECORD_HASH_MISMATCH": "restore the exact dataset content",
                "DATASET_VERIFICATION_UNAVAILABLE": "configure the immutable dataset verifier",
                "QUALIFICATION_MISSING": "attach the exact dataset qualification record",
                "QUALIFICATION_INVALID": "repair the referenced qualification record",
                "QUALIFICATION_HASH_MISMATCH": "restore the exact qualification record",
                "QUALIFICATION_VERIFICATION_UNAVAILABLE": (
                    "configure the immutable qualification verifier"
                ),
                "QUALIFICATION_DATASET_MISMATCH": "bind qualification to the consumed dataset",
                "QUALIFICATION_POLICY_MISMATCH": (
                    "use the qualification policy declared by the experiment"
                ),
                "DATASET_QUALIFICATION_REJECTED": "obtain a passing dataset qualification",
                "EMPIRICAL_ELIGIBILITY_BLOCKED": "obtain empirical dataset eligibility",
                "OOS_DATA_CONTAMINATED": "repeat evaluation with a pristine locked OOS boundary",
                "DATASET_CONTENT_HASH_MISMATCH": "restore the exact dataset content",
                "DATASET_MANIFEST_HASH_MISMATCH": "restore the exact dataset manifest",
                "NORMALIZED_DATASET_UNVERIFIED": "verify the normalized dataset artifact",
                "EVALUATION_SLICE_MISSING": (
                    "bind a slice containing the declared evaluation period"
                ),
            }[code]
            for code in reasons
            if code
            in {
                "MISSING_EVIDENCE",
                "EVIDENCE_INTEGRITY_UNVERIFIED",
                "APPROVAL_REQUIRED",
                "OOS_STATUS_REQUIRED",
                "VALIDATION_NOT_PASSED",
                "STRATEGY_DISABLED",
                "ARTIFACT_INTEGRITY_UNVERIFIED",
                "DATASET_RECORD_MISSING",
                "DATASET_RECORD_INVALID",
                "DATASET_RECORD_HASH_MISMATCH",
                "DATASET_VERIFICATION_UNAVAILABLE",
                "QUALIFICATION_MISSING",
                "QUALIFICATION_INVALID",
                "QUALIFICATION_HASH_MISMATCH",
                "QUALIFICATION_VERIFICATION_UNAVAILABLE",
                "QUALIFICATION_DATASET_MISMATCH",
                "QUALIFICATION_POLICY_MISMATCH",
                "DATASET_QUALIFICATION_REJECTED",
                "EMPIRICAL_ELIGIBILITY_BLOCKED",
                "OOS_DATA_CONTAMINATED",
                "DATASET_CONTENT_HASH_MISMATCH",
                "DATASET_MANIFEST_HASH_MISMATCH",
                "NORMALIZED_DATASET_UNVERIFIED",
                "EVALUATION_SLICE_MISSING",
            }
        )
        missing = tuple(
            code
            for code in reasons
            if code
            in {
                "MISSING_EVIDENCE",
                "APPROVAL_REQUIRED",
                "OOS_STATUS_REQUIRED",
                "DATASET_RECORD_MISSING",
                "DATASET_VERIFICATION_UNAVAILABLE",
                "QUALIFICATION_MISSING",
                "QUALIFICATION_VERIFICATION_UNAVAILABLE",
            }
        )
        target = (
            state.stage.value.upper()
            if state and state.stage in (StrategyStage.SHADOW, StrategyStage.PAPER)
            else "PAPER"
        )
        if decision is EvaluationDecision.ALLOW:
            summary = f"Eligible for recorded {target} stage."
        else:
            detail = ", ".join(reasons) if reasons else "UNAVAILABLE"
            summary = f"Blocked from {target}: {detail}."
        return ProvenanceEligibilityExplanation(
            strategy_id=state.strategy_id if state else "",
            strategy_version=state.strategy_version if state else "",
            artifact_hash=artifact_hash,
            stage=state.stage if state else None,
            revision=state.revision if state else None,
            disabled=state.disabled if state else None,
            health=state.health if state else None,
            decision=decision,
            active_approvals=active,
            revoked_approvals=revoked,
            evidence=evidence_summaries,
            policy_references=policy_refs,
            missing_prerequisites=tuple(dict.fromkeys(missing)),
            blocking_reason_codes=tuple(dict.fromkeys(reasons)),
            review_requirements=tuple(dict.fromkeys(review_requirements)),
            summary=summary,
            query_timestamp=snapshot.query_timestamp,
            consistency_boundary=self._CONSISTENCY,
            data_binding_status=binding_status,
            dataset_status=dataset_status,
            dataset_content_hash=dataset_content_hash,
            dataset_manifest_hash=dataset_manifest_hash,
            qualification_id=qualification_id,
            qualification_status=qualification_status,
            qualification_verdict=qualification_verdict,
            empirical_eligibility=empirical_eligibility,
            oos_data_status=oos_data_status,
        )

    @staticmethod
    def _policy_references(snapshot: _DatabaseSnapshot) -> tuple[tuple[str, str], ...]:
        refs: set[tuple[str, str]] = set()
        for approval_record in snapshot.approvals:
            if approval_record.value is not None:
                refs.add(
                    (
                        approval_record.value.policy_id,
                        approval_record.value.policy_version,
                    )
                )
        for event_record in snapshot.events:
            if event_record.value is not None:
                refs.add((event_record.value.policy_id, event_record.value.policy_version))
        return tuple(sorted(refs))

    def _authorize(self, actor_id: str) -> None:
        try:
            actor = self._actors.resolve(actor_id)
        except LifecycleError as exc:
            raise ProvenanceAuthorizationError("provenance read is unauthorized") from exc
        if not actor.can(ActorCapability.PROVENANCE_READ):
            raise ProvenanceAuthorizationError("provenance read is unauthorized")

    def _now(self) -> datetime:
        value = self._clock()
        require_utc(value)
        return value

    def _unavailable_explanation(
        self, strategy_id: str, strategy_version: str
    ) -> ProvenanceEligibilityExplanation:
        return ProvenanceEligibilityExplanation(
            strategy_id,
            strategy_version,
            None,
            None,
            None,
            None,
            None,
            EvaluationDecision.REJECT,
            (),
            (),
            (),
            (),
            ("STRATEGY_STATE_UNAVAILABLE",),
            ("STRATEGY_STATE_UNAVAILABLE",),
            (),
            "Blocked from PAPER: STRATEGY_STATE_UNAVAILABLE.",
            self._now(),
            self._CONSISTENCY,
        )

    @staticmethod
    def _verified_revocations(
        reads: Sequence[_StoredRevocationRead],
    ) -> tuple[RevocationRecord, ...]:
        return tuple(item.value for item in reads if item.value is not None)

    @staticmethod
    def _revocation_state(
        records: Sequence[RevocationRecord], target_id: str | None, event_time: datetime
    ) -> tuple[bool | None, bool | None]:
        if target_id is None:
            return None, None
        matching = [item for item in records if item.target_id == target_id]
        if not matching:
            return False, False
        return True, any(item.revoked_at <= event_time for item in matching)

    @staticmethod
    def _write_export(destination: Path, data: bytes) -> None:
        path = destination.expanduser()
        if os.path.lexists(path):
            if path.is_symlink() or path.is_dir():
                raise ProvenanceError("provenance export destination is not a safe regular file")
            if path.read_bytes() != data:
                raise ProvenanceError(
                    "provenance export destination already contains different data"
                )
            return
        if not path.parent.exists() or path.parent.is_symlink():
            raise ProvenanceError(
                "provenance export parent must be an existing non-symlink directory"
            )
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.provenance.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, path)
        except FileExistsError:
            if path.exists() and path.read_bytes() != data:
                raise ProvenanceError(
                    "provenance export destination already contains different data"
                ) from None
        except OSError as exc:
            raise ProvenanceError("unable to write provenance export") from exc
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


__all__ = [
    "ApprovalEligibilitySummary",
    "EvidenceClassification",
    "EvidenceEligibilitySummary",
    "ExperimentDataEligibilityExplanation",
    "ExperimentProvenanceReader",
    "GovernanceProvenanceReader",
    "HistoricalEventExplanation",
    "IntegrityStatus",
    "ProvenanceAuthorizationError",
    "ProvenanceError",
    "ProvenanceExport",
    "ProvenanceFinding",
    "ProvenanceGraph",
    "ProvenanceNode",
    "ProvenanceNodeType",
    "ProvenanceRelationship",
    "ProvenanceRelationshipType",
    "ProvenanceTimestamp",
    "ProvenanceEligibilityExplanation",
    "ProvenanceQueryService",
]
