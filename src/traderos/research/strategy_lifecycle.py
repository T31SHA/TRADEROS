"""Versioned strategy artifacts and the pure governed lifecycle.

This module is deliberately independent of paper execution.  It describes
what a strategy is, what evidence is attached to that exact immutable version,
and whether a requested lifecycle transition is admissible.  Persistence and
actor resolution live in :mod:`traderos.research.strategy_service`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from math import isfinite
from typing import cast

from traderos.data.time import require_utc
from traderos.research.models import ResearchDecision, ResearchError, ResearchScope, TemporalRange


class LifecycleError(ResearchError):
    """Raised when an artifact or governed lifecycle operation is invalid."""


class StrategyStage(StrEnum):
    CANDIDATE = "candidate"
    RESEARCH = "research"
    VALIDATION = "validation"
    SHADOW = "shadow"
    PAPER = "paper"
    CANARY = "canary"
    ACTIVE = "active"
    RETIRED = "retired"


class StrategyHealth(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    WEAKENING = "weakening"
    DEGRADED = "degraded"
    FAILED = "failed"


class ActorCapability(StrEnum):
    RESEARCH = "research"
    TRANSITION = "transition"
    APPROVE = "approve"
    RETIRE = "retire"
    DISABLE = "disable"
    PROVENANCE_READ = "provenance_read"


class EvaluationDecision(StrEnum):
    ALLOW = "allow"
    HOLD = "hold"
    REJECT = "reject"


class EvidenceOosStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    PRISTINE = "pristine"
    CONTAMINATED = "contaminated"
    UNAVAILABLE = "unavailable"


class LifecycleReason(StrEnum):
    REGISTERED = "registered"
    RESEARCH_STARTED = "research_started"
    EVIDENCE_ATTACHED = "evidence_attached"
    VALIDATION_RECORDED = "validation_recorded"
    EXPLICIT_APPROVAL = "explicit_approval"
    GOVERNED_DEMOTION = "governed_demotion"
    RETIRED = "retired"
    STRATEGY_DISABLED = "strategy_disabled"


def _require_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise LifecycleError(f"{field} must be non-blank")


def _tagged_value(value: object) -> dict[str, object]:
    """Return a type-tagged JSON value, rejecting unsafe numerics and objects."""

    if value is None:
        return {"type": "null", "value": None}
    if type(value) is bool:
        return {"type": "boolean", "value": value}
    if type(value) is int:
        return {"type": "integer", "value": value}
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise LifecycleError("strategy parameters must not contain non-finite decimals")
        return {"type": "decimal", "value": str(value)}
    if type(value) is float:
        if not isfinite(value):
            raise LifecycleError("strategy parameters must not contain non-finite floats")
        return {"type": "float", "value": value}
    if isinstance(value, str):
        return {"type": "string", "value": value}
    if isinstance(value, Mapping):
        entries: list[list[object]] = []
        for key in sorted(value, key=lambda item: str(item)):
            if not isinstance(key, str) or not key.strip():
                raise LifecycleError("strategy parameter mappings require non-blank string keys")
            entries.append([key, _tagged_value(value[key])])
        return {"type": "mapping", "value": entries}
    if isinstance(value, (tuple, list)):
        return {"type": "list", "value": [_tagged_value(item) for item in value]}
    raise LifecycleError(f"unsupported strategy parameter type: {type(value).__name__}")


def _untagged_value(payload: object) -> object:
    if not isinstance(payload, dict) or set(payload) != {"type", "value"}:
        raise LifecycleError("typed parameter value is malformed")
    kind = payload["type"]
    value = payload["value"]
    if kind == "null":
        if value is not None:
            raise LifecycleError("typed null parameter has a value")
        return None
    if kind == "boolean":
        if type(value) is not bool:
            raise LifecycleError("typed boolean parameter is malformed")
        return value
    if kind == "integer":
        if type(value) is not int:
            raise LifecycleError("typed integer parameter is malformed")
        return value
    if kind == "float":
        if type(value) is not float or not isfinite(value):
            raise LifecycleError("typed float parameter is malformed")
        return value
    if kind == "decimal":
        if not isinstance(value, str):
            raise LifecycleError("typed decimal parameter is malformed")
        decimal_value = Decimal(value)
        if not decimal_value.is_finite():
            raise LifecycleError("typed decimal parameter is non-finite")
        return decimal_value
    if kind == "string":
        if not isinstance(value, str):
            raise LifecycleError("typed string parameter is malformed")
        return value
    if kind == "list":
        if not isinstance(value, list):
            raise LifecycleError("typed list parameter is malformed")
        return tuple(_untagged_value(item) for item in value)
    if kind == "mapping":
        if not isinstance(value, list):
            raise LifecycleError("typed mapping parameter is malformed")
        mapping_result: dict[str, object] = {}
        for item in value:
            if not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str):
                raise LifecycleError("typed mapping parameter entry is malformed")
            if item[0] in mapping_result:
                raise LifecycleError("typed mapping parameter keys must be unique")
            mapping_result[item[0]] = _untagged_value(item[1])
        return mapping_result
    raise LifecycleError(f"unknown typed parameter kind: {kind!r}")


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise LifecycleError("artifact contains a non-canonical JSON value") from exc


@dataclass(frozen=True)
class StrategyParameter:
    name: str
    value: object

    def __post_init__(self) -> None:
        _require_text(self.name, "parameter name")
        _tagged_value(self.value)

    def canonical(self) -> dict[str, object]:
        return {"name": self.name, "value": _tagged_value(self.value)}

    @classmethod
    def from_canonical(cls, payload: object) -> StrategyParameter:
        if not isinstance(payload, dict) or set(payload) != {"name", "value"}:
            raise LifecycleError("strategy parameter has unknown or missing fields")
        name = payload["name"]
        if not isinstance(name, str):
            raise LifecycleError("strategy parameter name is malformed")
        return cls(name, _untagged_value(payload["value"]))


@dataclass(frozen=True)
class FeatureDefinition:
    name: str
    version: str
    parameters: tuple[StrategyParameter, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.name, "feature name")
        _require_text(self.version, "feature version")
        names = tuple(item.name for item in self.parameters)
        if len(names) != len(set(names)):
            raise LifecycleError("feature parameter names must be unique")

    def canonical(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "parameters": [item.canonical() for item in self.parameters],
        }

    @classmethod
    def from_canonical(cls, payload: object) -> FeatureDefinition:
        if not isinstance(payload, dict) or set(payload) != {"name", "version", "parameters"}:
            raise LifecycleError("feature definition has unknown or missing fields")
        parameters = payload["parameters"]
        if not isinstance(parameters, list):
            raise LifecycleError("feature parameters must be a list")
        return cls(
            name=cast(str, payload["name"]),
            version=cast(str, payload["version"]),
            parameters=tuple(StrategyParameter.from_canonical(item) for item in parameters),
        )


@dataclass(frozen=True)
class RegisteredImplementation:
    implementation_id: str
    implementation_version: str

    def __post_init__(self) -> None:
        _require_text(self.implementation_id, "implementation id")
        _require_text(self.implementation_version, "implementation version")
        if any(character in self.implementation_id for character in ("/", "\\", " ")):
            raise LifecycleError("implementation id must be a registered logical identifier")

    def canonical(self) -> dict[str, str]:
        return {
            "implementation_id": self.implementation_id,
            "implementation_version": self.implementation_version,
        }


@dataclass(frozen=True)
class ResearchReference:
    reference_id: str
    dataset_hash: str
    scope: ResearchScope
    period: TemporalRange | None = None

    def __post_init__(self) -> None:
        _require_text(self.reference_id, "research reference id")
        _require_text(self.dataset_hash, "research reference dataset hash")

    def canonical(self) -> dict[str, object]:
        return {
            "reference_id": self.reference_id,
            "dataset_hash": self.dataset_hash,
            "scope": self.scope.value,
            "period": (
                {"start": self.period.start.isoformat(), "end": self.period.end.isoformat()}
                if self.period is not None
                else None
            ),
        }


def _reference_from_canonical(payload: object) -> ResearchReference:
    if not isinstance(payload, dict) or set(payload) != {
        "reference_id",
        "dataset_hash",
        "scope",
        "period",
    }:
        raise LifecycleError("research reference has unknown or missing fields")
    period_payload = payload["period"]
    period: TemporalRange | None = None
    if period_payload is not None:
        if not isinstance(period_payload, dict) or set(period_payload) != {"start", "end"}:
            raise LifecycleError("research reference period is malformed")
        try:
            period = TemporalRange(
                datetime.fromisoformat(cast(str, period_payload["start"])),
                datetime.fromisoformat(cast(str, period_payload["end"])),
            )
        except (TypeError, ValueError) as exc:
            raise LifecycleError("research reference period is malformed") from exc
    try:
        scope = ResearchScope(cast(str, payload["scope"]))
    except ValueError as exc:
        raise LifecycleError("research reference scope is unknown") from exc
    return ResearchReference(
        reference_id=cast(str, payload["reference_id"]),
        dataset_hash=cast(str, payload["dataset_hash"]),
        scope=scope,
        period=period,
    )


@dataclass(frozen=True)
class StrategyArtifact:
    """Immutable content-addressed strategy version.

    ``strategy_id`` and ``strategy_version`` are stable lookup identity.
    ``code_identity`` records the immutable implementation content/build
    identity; the operational runtime binding keeps its implementation
    locator separate and verifies the loaded source manifest against this
    value. The ``artifact_hash`` is derived from the complete canonical
    payload and is a separate integrity identity. No result or approval is
    included in this payload, avoiding circular hashes.
    """

    strategy_id: str
    strategy_version: str
    family_id: str
    author: str
    implementation: RegisteredImplementation
    instruments: tuple[str, ...]
    timeframes: tuple[str, ...]
    parameters: tuple[StrategyParameter, ...]
    features: tuple[FeatureDefinition, ...]
    training_reference: ResearchReference | None
    validation_reference: ResearchReference | None
    oos_reference: ResearchReference | None
    forecast_semantics: str
    holding_period: str | None
    turnover_assumption: Decimal | None
    cost_model_reference: str | None
    capacity_model_reference: str | None
    regime_dependencies: tuple[str, ...]
    risk_dependencies: tuple[str, ...]
    known_failure_modes: tuple[str, ...]
    code_identity: str
    dataset_identity: str | None
    configuration_identity: str | None
    environment_reference: str | None
    unavailable_evidence: tuple[str, ...] = ()
    predecessor_artifact_hash: str | None = None
    schema_version: str = "strategy-artifact.v1"

    def __post_init__(self) -> None:
        if self.schema_version != "strategy-artifact.v1":
            raise LifecycleError("unsupported strategy artifact schema version")
        for field, value in (
            ("strategy id", self.strategy_id),
            ("strategy version", self.strategy_version),
            ("family id", self.family_id),
            ("author", self.author),
            ("forecast semantics", self.forecast_semantics),
            ("code identity", self.code_identity),
        ):
            _require_text(value, field)
        if not self.instruments or any(not item.strip() for item in self.instruments):
            raise LifecycleError("strategy artifact requires non-blank instruments")
        if not self.timeframes or any(not item.strip() for item in self.timeframes):
            raise LifecycleError("strategy artifact requires non-blank timeframes")
        if len(set(self.instruments)) != len(self.instruments):
            raise LifecycleError("strategy artifact instruments must be unique")
        parameter_names = tuple(item.name for item in self.parameters)
        if len(parameter_names) != len(set(parameter_names)):
            raise LifecycleError("strategy artifact parameter names must be unique")
        feature_identities = tuple(_canonical_json(item.canonical()) for item in self.features)
        if len(feature_identities) != len(set(feature_identities)):
            raise LifecycleError("strategy artifact feature definitions must be unique")
        if self.turnover_assumption is not None and (
            not self.turnover_assumption.is_finite() or self.turnover_assumption < 0
        ):
            raise LifecycleError("turnover assumption must be finite and non-negative")
        for field, reference_value in (
            ("dataset identity", self.dataset_identity),
            ("configuration identity", self.configuration_identity),
            ("environment reference", self.environment_reference),
            ("predecessor artifact hash", self.predecessor_artifact_hash),
        ):
            if reference_value is not None:
                _require_text(reference_value, field)
        if any(not item.strip() for item in self.unavailable_evidence):
            raise LifecycleError("unavailable evidence dimensions must be non-blank")

    def canonical(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "family_id": self.family_id,
            "author": self.author,
            "implementation": self.implementation.canonical(),
            "instruments": list(self.instruments),
            "timeframes": list(self.timeframes),
            "parameters": [item.canonical() for item in self.parameters],
            "features": [item.canonical() for item in self.features],
            "training_reference": (
                self.training_reference.canonical() if self.training_reference is not None else None
            ),
            "validation_reference": (
                self.validation_reference.canonical()
                if self.validation_reference is not None
                else None
            ),
            "oos_reference": (
                self.oos_reference.canonical() if self.oos_reference is not None else None
            ),
            "forecast_semantics": self.forecast_semantics,
            "holding_period": self.holding_period,
            "turnover_assumption": (
                {"type": "decimal", "value": str(self.turnover_assumption)}
                if self.turnover_assumption is not None
                else None
            ),
            "cost_model_reference": self.cost_model_reference,
            "capacity_model_reference": self.capacity_model_reference,
            "regime_dependencies": list(self.regime_dependencies),
            "risk_dependencies": list(self.risk_dependencies),
            "known_failure_modes": list(self.known_failure_modes),
            "code_identity": self.code_identity,
            "dataset_identity": self.dataset_identity,
            "configuration_identity": self.configuration_identity,
            "environment_reference": self.environment_reference,
            "unavailable_evidence": list(self.unavailable_evidence),
            "predecessor_artifact_hash": self.predecessor_artifact_hash,
        }

    @property
    def artifact_hash(self) -> str:
        return hashlib.sha256(_canonical_json(self.canonical())).hexdigest()

    @classmethod
    def from_canonical(
        cls, payload: object, *, expected_hash: str | None = None
    ) -> StrategyArtifact:
        if not isinstance(payload, dict):
            raise LifecycleError("strategy artifact payload must be an object")
        expected_fields = set(cls.__dataclass_fields__) - {"artifact_hash"}
        if set(payload) != expected_fields:
            raise LifecycleError("strategy artifact has unknown or missing fields")
        implementation_payload = payload["implementation"]
        if not isinstance(implementation_payload, dict) or set(implementation_payload) != {
            "implementation_id",
            "implementation_version",
        }:
            raise LifecycleError("registered implementation is malformed")
        parameters = payload["parameters"]
        features = payload["features"]
        if not isinstance(parameters, list) or not isinstance(features, list):
            raise LifecycleError("strategy parameters and features must be lists")
        turnover_payload = payload["turnover_assumption"]
        turnover: Decimal | None = None
        if turnover_payload is not None:
            turnover_value = _untagged_value(turnover_payload)
            if not isinstance(turnover_value, Decimal):
                raise LifecycleError("turnover assumption must be a finite decimal")
            turnover = turnover_value
        artifact = cls(
            schema_version=cast(str, payload["schema_version"]),
            strategy_id=cast(str, payload["strategy_id"]),
            strategy_version=cast(str, payload["strategy_version"]),
            family_id=cast(str, payload["family_id"]),
            author=cast(str, payload["author"]),
            implementation=RegisteredImplementation(
                cast(str, implementation_payload["implementation_id"]),
                cast(str, implementation_payload["implementation_version"]),
            ),
            instruments=tuple(cast(list[str], payload["instruments"])),
            timeframes=tuple(cast(list[str], payload["timeframes"])),
            parameters=tuple(StrategyParameter.from_canonical(item) for item in parameters),
            features=tuple(FeatureDefinition.from_canonical(item) for item in features),
            training_reference=(
                _reference_from_canonical(payload["training_reference"])
                if payload["training_reference"] is not None
                else None
            ),
            validation_reference=(
                _reference_from_canonical(payload["validation_reference"])
                if payload["validation_reference"] is not None
                else None
            ),
            oos_reference=(
                _reference_from_canonical(payload["oos_reference"])
                if payload["oos_reference"] is not None
                else None
            ),
            forecast_semantics=cast(str, payload["forecast_semantics"]),
            holding_period=cast(str | None, payload["holding_period"]),
            turnover_assumption=turnover,
            cost_model_reference=cast(str | None, payload["cost_model_reference"]),
            capacity_model_reference=cast(str | None, payload["capacity_model_reference"]),
            regime_dependencies=tuple(cast(list[str], payload["regime_dependencies"])),
            risk_dependencies=tuple(cast(list[str], payload["risk_dependencies"])),
            known_failure_modes=tuple(cast(list[str], payload["known_failure_modes"])),
            code_identity=cast(str, payload["code_identity"]),
            dataset_identity=cast(str | None, payload["dataset_identity"]),
            configuration_identity=cast(str | None, payload["configuration_identity"]),
            environment_reference=cast(str | None, payload["environment_reference"]),
            unavailable_evidence=tuple(cast(list[str], payload["unavailable_evidence"])),
            predecessor_artifact_hash=cast(str | None, payload["predecessor_artifact_hash"]),
        )
        if expected_hash is not None and artifact.artifact_hash != expected_hash:
            raise LifecycleError("strategy artifact hash mismatch")
        return artifact


@dataclass(frozen=True)
class EvidenceAttachment:
    evidence_id: str
    artifact_hash: str
    dataset_hashes: tuple[str, ...]
    experiment_id: str
    result_hash: str
    validation_policy_id: str
    validation_policy_version: str
    oos_status: EvidenceOosStatus
    validation_verdict: ResearchDecision | None
    warnings: tuple[str, ...]
    unavailable_dimensions: tuple[str, ...]
    empirical_eligible: bool
    deterministic_test_fixture: bool
    reviewer_identity: str | None = None
    schema_version: str = "strategy-evidence.v1"

    def __post_init__(self) -> None:
        if self.schema_version != "strategy-evidence.v1":
            raise LifecycleError("unsupported strategy evidence schema version")
        for field, value in (
            ("evidence id", self.evidence_id),
            ("artifact hash", self.artifact_hash),
            ("experiment id", self.experiment_id),
            ("result hash", self.result_hash),
            ("validation policy id", self.validation_policy_id),
            ("validation policy version", self.validation_policy_version),
        ):
            _require_text(value, field)
        if not self.dataset_hashes or any(not value.strip() for value in self.dataset_hashes):
            raise LifecycleError("evidence requires dataset identities")
        if self.deterministic_test_fixture and self.empirical_eligible:
            raise LifecycleError("fixture evidence cannot be empirically eligible")
        if self.reviewer_identity is not None:
            _require_text(self.reviewer_identity, "reviewer identity")
        if any(not item.strip() for item in (*self.warnings, *self.unavailable_dimensions)):
            raise LifecycleError("evidence warnings and unavailable dimensions must be non-blank")

    def canonical(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "evidence_id": self.evidence_id,
            "artifact_hash": self.artifact_hash,
            "dataset_hashes": list(self.dataset_hashes),
            "experiment_id": self.experiment_id,
            "result_hash": self.result_hash,
            "validation_policy_id": self.validation_policy_id,
            "validation_policy_version": self.validation_policy_version,
            "oos_status": self.oos_status.value,
            "validation_verdict": (
                self.validation_verdict.value if self.validation_verdict is not None else None
            ),
            "warnings": list(self.warnings),
            "unavailable_dimensions": list(self.unavailable_dimensions),
            "empirical_eligible": self.empirical_eligible,
            "deterministic_test_fixture": self.deterministic_test_fixture,
            "reviewer_identity": self.reviewer_identity,
        }

    @property
    def evidence_hash(self) -> str:
        return hashlib.sha256(_canonical_json(self.canonical())).hexdigest()

    @classmethod
    def from_canonical(
        cls, payload: object, *, expected_hash: str | None = None
    ) -> EvidenceAttachment:
        if not isinstance(payload, dict):
            raise LifecycleError("evidence payload must be an object")
        expected_fields = set(cls.__dataclass_fields__) - {"evidence_hash"}
        if set(payload) != expected_fields:
            raise LifecycleError("evidence has unknown or missing fields")
        verdict_payload = payload["validation_verdict"]
        verdict = (
            ResearchDecision(cast(str, verdict_payload)) if verdict_payload is not None else None
        )
        evidence = cls(
            schema_version=cast(str, payload["schema_version"]),
            evidence_id=cast(str, payload["evidence_id"]),
            artifact_hash=cast(str, payload["artifact_hash"]),
            dataset_hashes=tuple(cast(list[str], payload["dataset_hashes"])),
            experiment_id=cast(str, payload["experiment_id"]),
            result_hash=cast(str, payload["result_hash"]),
            validation_policy_id=cast(str, payload["validation_policy_id"]),
            validation_policy_version=cast(str, payload["validation_policy_version"]),
            oos_status=EvidenceOosStatus(cast(str, payload["oos_status"])),
            validation_verdict=verdict,
            warnings=tuple(cast(list[str], payload["warnings"])),
            unavailable_dimensions=tuple(cast(list[str], payload["unavailable_dimensions"])),
            empirical_eligible=cast(bool, payload["empirical_eligible"]),
            deterministic_test_fixture=cast(bool, payload["deterministic_test_fixture"]),
            reviewer_identity=cast(str | None, payload["reviewer_identity"]),
        )
        if expected_hash is not None and evidence.evidence_hash != expected_hash:
            raise LifecycleError("strategy evidence hash mismatch")
        return evidence


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    artifact_hash: str
    evidence_id: str
    target_stage: StrategyStage
    actor_id: str
    policy_id: str
    policy_version: str
    reason: str
    approved_at: datetime
    schema_version: str = "strategy-approval.v1"

    def __post_init__(self) -> None:
        if self.schema_version != "strategy-approval.v1":
            raise LifecycleError("unsupported strategy approval schema version")
        for field, value in (
            ("approval id", self.approval_id),
            ("artifact hash", self.artifact_hash),
            ("evidence id", self.evidence_id),
            ("actor id", self.actor_id),
            ("policy id", self.policy_id),
            ("policy version", self.policy_version),
            ("approval reason", self.reason),
        ):
            _require_text(value, field)
        require_utc(self.approved_at)
        if self.target_stage not in (StrategyStage.SHADOW, StrategyStage.PAPER):
            raise LifecycleError("approvals are only issued for SHADOW or PAPER")

    def canonical(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "approval_id": self.approval_id,
            "artifact_hash": self.artifact_hash,
            "evidence_id": self.evidence_id,
            "target_stage": self.target_stage.value,
            "actor_id": self.actor_id,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "reason": self.reason,
            "approved_at": self.approved_at.isoformat(),
        }

    @property
    def approval_hash(self) -> str:
        return hashlib.sha256(_canonical_json(self.canonical())).hexdigest()

    @classmethod
    def from_canonical(cls, payload: object) -> ApprovalRecord:
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "approval_id",
            "artifact_hash",
            "evidence_id",
            "target_stage",
            "actor_id",
            "policy_id",
            "policy_version",
            "reason",
            "approved_at",
        }:
            raise LifecycleError("approval has unknown or missing fields")
        try:
            approved_at = datetime.fromisoformat(cast(str, payload["approved_at"]))
            target_stage = StrategyStage(cast(str, payload["target_stage"]))
        except (TypeError, ValueError) as exc:
            raise LifecycleError("approval timestamp or target stage is malformed") from exc
        return cls(
            schema_version=cast(str, payload["schema_version"]),
            approval_id=cast(str, payload["approval_id"]),
            artifact_hash=cast(str, payload["artifact_hash"]),
            evidence_id=cast(str, payload["evidence_id"]),
            target_stage=target_stage,
            actor_id=cast(str, payload["actor_id"]),
            policy_id=cast(str, payload["policy_id"]),
            policy_version=cast(str, payload["policy_version"]),
            reason=cast(str, payload["reason"]),
            approved_at=approved_at,
        )


@dataclass(frozen=True)
class TrustedActor:
    actor_id: str
    capabilities: frozenset[ActorCapability]

    def can(self, capability: ActorCapability) -> bool:
        return capability in self.capabilities


class ActorDirectory:
    """Internal trust boundary used until a production identity provider exists."""

    def __init__(self, actors: Mapping[str, Sequence[ActorCapability]]) -> None:
        if not actors:
            raise LifecycleError("actor directory must not be empty")
        self._actors = {
            actor_id: frozenset(capabilities)
            for actor_id, capabilities in actors.items()
            if actor_id.strip()
        }
        if len(self._actors) != len(actors):
            raise LifecycleError("actor identities must be non-blank")

    def resolve(self, actor_id: str) -> TrustedActor:
        try:
            return TrustedActor(actor_id, self._actors[actor_id])
        except KeyError as exc:
            raise LifecycleError("actor is not trusted by the internal actor directory") from exc


@dataclass(frozen=True)
class LifecyclePolicy:
    policy_id: str
    version: str
    live_capability_available: bool = False

    def __post_init__(self) -> None:
        _require_text(self.policy_id, "lifecycle policy id")
        _require_text(self.version, "lifecycle policy version")
        if self.live_capability_available:
            raise LifecycleError("live capability is not implemented in this milestone")


@dataclass(frozen=True)
class StrategyState:
    strategy_id: str
    strategy_version: str
    artifact_hash: str
    stage: StrategyStage
    health: StrategyHealth
    revision: int
    disabled: bool
    disable_reason: str | None
    updated_at: datetime
    last_event_id: str | None

    def __post_init__(self) -> None:
        require_utc(self.updated_at)
        if self.revision < 0:
            raise LifecycleError("strategy state revision must be non-negative")
        if self.disabled and not self.disable_reason:
            raise LifecycleError("disabled strategy state requires a reason")


@dataclass(frozen=True)
class LifecycleEvent:
    event_id: str
    strategy_id: str
    strategy_version: str
    artifact_hash: str
    event_type: str
    previous_stage: StrategyStage
    resulting_stage: StrategyStage
    expected_revision: int
    resulting_revision: int
    actor_id: str
    policy_id: str
    policy_version: str
    evidence_id: str | None
    approval_id: str | None
    reason_code: str
    reason: str
    occurred_at: datetime
    idempotency_key: str
    correlation_id: str

    def __post_init__(self) -> None:
        require_utc(self.occurred_at)
        for field, value in (
            ("event id", self.event_id),
            ("strategy id", self.strategy_id),
            ("strategy version", self.strategy_version),
            ("artifact hash", self.artifact_hash),
            ("event type", self.event_type),
            ("actor id", self.actor_id),
            ("policy id", self.policy_id),
            ("policy version", self.policy_version),
            ("reason code", self.reason_code),
            ("reason", self.reason),
            ("idempotency key", self.idempotency_key),
            ("correlation id", self.correlation_id),
        ):
            _require_text(value, field)
        if self.expected_revision < 0 or self.resulting_revision != self.expected_revision + 1:
            raise LifecycleError("lifecycle event revisions are not monotonic")


@dataclass(frozen=True)
class RevocationRecord:
    """Immutable fact that invalidates one evidence or approval record."""

    revocation_id: str
    target_type: str
    target_id: str
    actor_id: str
    reason: str
    revoked_at: datetime
    idempotency_key: str

    def __post_init__(self) -> None:
        require_utc(self.revoked_at)
        for field, value in (
            ("revocation id", self.revocation_id),
            ("revocation target type", self.target_type),
            ("revocation target id", self.target_id),
            ("revocation actor id", self.actor_id),
            ("revocation reason", self.reason),
            ("revocation idempotency key", self.idempotency_key),
        ):
            _require_text(value, field)


@dataclass(frozen=True)
class LifecycleEvaluation:
    decision: EvaluationDecision
    reason_codes: tuple[str, ...]
    explanation: str


_ALLOWED_TRANSITIONS: dict[StrategyStage, frozenset[StrategyStage]] = {
    StrategyStage.CANDIDATE: frozenset({StrategyStage.RESEARCH, StrategyStage.RETIRED}),
    StrategyStage.RESEARCH: frozenset({StrategyStage.VALIDATION, StrategyStage.RETIRED}),
    StrategyStage.VALIDATION: frozenset({StrategyStage.SHADOW, StrategyStage.RETIRED}),
    StrategyStage.SHADOW: frozenset(
        {StrategyStage.PAPER, StrategyStage.VALIDATION, StrategyStage.RETIRED}
    ),
    StrategyStage.PAPER: frozenset({StrategyStage.SHADOW, StrategyStage.RETIRED}),
    StrategyStage.CANARY: frozenset(),
    StrategyStage.ACTIVE: frozenset(),
    StrategyStage.RETIRED: frozenset(),
}


def allowed_transitions() -> dict[StrategyStage, frozenset[StrategyStage]]:
    """Return a copy of the explicit lifecycle transition table."""

    return dict(_ALLOWED_TRANSITIONS)


def evaluate_current_eligibility(
    *,
    state: StrategyState,
    artifact: StrategyArtifact,
    evidence: EvidenceAttachment | None,
    approval: ApprovalRecord | None,
    evidence_revoked: bool = False,
    approval_revoked: bool = False,
) -> LifecycleEvaluation:
    """Derive current operational eligibility without proposing a transition."""

    # Recorded invalidation is more specific than a broad disablement.  This
    # ordering keeps the explanation useful when both controls are present.
    if evidence is not None and evidence_revoked:
        return LifecycleEvaluation(
            EvaluationDecision.REJECT, ("EVIDENCE_REVOKED",), "evidence is revoked"
        )
    if approval is not None and approval_revoked:
        return LifecycleEvaluation(
            EvaluationDecision.REJECT, ("APPROVAL_REVOKED",), "approval is revoked"
        )
    if state.health is StrategyHealth.FAILED:
        return LifecycleEvaluation(
            EvaluationDecision.REJECT,
            ("STRATEGY_HEALTH_FAILED",),
            "strategy health is explicitly failed",
        )
    if state.disabled:
        return LifecycleEvaluation(
            EvaluationDecision.HOLD,
            ("STRATEGY_DISABLED",),
            "disabled strategy versions cannot receive new operational eligibility",
        )
    if state.stage not in (StrategyStage.SHADOW, StrategyStage.PAPER):
        return LifecycleEvaluation(
            EvaluationDecision.HOLD,
            ("NOT_OPERATIONAL_STAGE",),
            "the strategy version is not in an operationally eligible stage",
        )
    if evidence is None:
        return LifecycleEvaluation(
            EvaluationDecision.HOLD, ("MISSING_EVIDENCE",), "required evidence is unavailable"
        )
    if evidence.artifact_hash != artifact.artifact_hash:
        return LifecycleEvaluation(
            EvaluationDecision.REJECT,
            ("EVIDENCE_ARTIFACT_MISMATCH",),
            "evidence is bound to a different strategy artifact",
        )
    if evidence.oos_status is EvidenceOosStatus.CONTAMINATED:
        return LifecycleEvaluation(
            EvaluationDecision.REJECT,
            ("CONTAMINATED_OOS",),
            "contaminated OOS evidence fails closed",
        )
    if evidence.oos_status is not EvidenceOosStatus.PRISTINE:
        return LifecycleEvaluation(
            EvaluationDecision.HOLD,
            ("OOS_STATUS_REQUIRED",),
            "operational eligibility requires an explicit pristine OOS status",
        )
    if evidence.deterministic_test_fixture or not evidence.empirical_eligible:
        return LifecycleEvaluation(
            EvaluationDecision.REJECT,
            ("FIXTURE_OR_EMPIRICAL_ELIGIBILITY_REQUIRED",),
            "test-fixture or non-empirical evidence cannot authorize operational use",
        )
    if evidence.validation_verdict in (ResearchDecision.KILL, ResearchDecision.INVALID):
        return LifecycleEvaluation(
            EvaluationDecision.REJECT,
            ("VALIDATION_REJECTED",),
            "validation evidence has a rejecting research verdict",
        )
    if evidence.validation_verdict is None:
        return LifecycleEvaluation(
            EvaluationDecision.HOLD,
            ("VALIDATION_VERDICT_UNAVAILABLE",),
            "validation verdict is unavailable",
        )
    if evidence.validation_verdict is not ResearchDecision.PASS:
        return LifecycleEvaluation(
            EvaluationDecision.HOLD,
            ("VALIDATION_NOT_PASSED",),
            "operational eligibility requires an explicit passing validation verdict",
        )
    if approval is None:
        return LifecycleEvaluation(
            EvaluationDecision.HOLD, ("APPROVAL_REQUIRED",), "explicit approval is required"
        )
    if (
        approval.artifact_hash != artifact.artifact_hash
        or approval.evidence_id != evidence.evidence_id
    ):
        return LifecycleEvaluation(
            EvaluationDecision.REJECT,
            ("APPROVAL_SCOPE_MISMATCH",),
            "approval does not match the exact artifact and evidence",
        )
    if approval.target_stage is not state.stage:
        return LifecycleEvaluation(
            EvaluationDecision.REJECT,
            ("APPROVAL_SCOPE_MISMATCH",),
            "approval does not match the current lifecycle stage",
        )
    return LifecycleEvaluation(
        EvaluationDecision.ALLOW, (), "current operational eligibility is allowed"
    )


def evaluate_transition(
    *,
    state: StrategyState,
    artifact: StrategyArtifact,
    target_stage: StrategyStage,
    actor: TrustedActor,
    policy: LifecyclePolicy,
    evidence: EvidenceAttachment | None = None,
    approval: ApprovalRecord | None = None,
    evidence_revoked: bool = False,
    approval_revoked: bool = False,
    reason: str,
) -> LifecycleEvaluation:
    """Pure, deterministic lifecycle decision; it never persists or executes."""

    if not reason.strip():
        return LifecycleEvaluation(
            EvaluationDecision.REJECT, ("REASON_REQUIRED",), "reason is required"
        )
    if target_stage in (StrategyStage.CANARY, StrategyStage.ACTIVE):
        return LifecycleEvaluation(
            EvaluationDecision.REJECT,
            ("LIVE_CAPABILITY_UNAVAILABLE",),
            "live lifecycle stages are unavailable while live execution is unimplemented",
        )
    if state.stage is StrategyStage.RETIRED:
        return LifecycleEvaluation(
            EvaluationDecision.REJECT, ("TERMINAL_STAGE",), "retired versions are terminal"
        )
    if target_stage not in _ALLOWED_TRANSITIONS[state.stage]:
        return LifecycleEvaluation(
            EvaluationDecision.REJECT,
            ("INVALID_TRANSITION",),
            f"transition {state.stage.value}->{target_stage.value} is not permitted",
        )
    required_capability = (
        ActorCapability.RETIRE
        if target_stage is StrategyStage.RETIRED
        else ActorCapability.RESEARCH
        if state.stage in (StrategyStage.CANDIDATE, StrategyStage.RESEARCH)
        else ActorCapability.TRANSITION
    )
    if not actor.can(required_capability):
        return LifecycleEvaluation(
            EvaluationDecision.REJECT,
            ("ACTOR_UNAUTHORIZED",),
            f"actor lacks {required_capability.value} capability",
        )
    if target_stage is StrategyStage.RETIRED:
        return LifecycleEvaluation(EvaluationDecision.ALLOW, (), "governed retirement is permitted")
    stage_order = {
        StrategyStage.CANDIDATE: 0,
        StrategyStage.RESEARCH: 1,
        StrategyStage.VALIDATION: 2,
        StrategyStage.SHADOW: 3,
        StrategyStage.PAPER: 4,
        StrategyStage.CANARY: 5,
        StrategyStage.ACTIVE: 6,
        StrategyStage.RETIRED: 7,
    }
    promotion = (
        target_stage in (StrategyStage.SHADOW, StrategyStage.PAPER)
        and stage_order[target_stage] > stage_order[state.stage]
    )
    if state.disabled and promotion:
        return LifecycleEvaluation(
            EvaluationDecision.HOLD,
            ("STRATEGY_DISABLED",),
            "disabled strategy versions cannot receive new operational eligibility",
        )

    evidence_required = promotion
    if state.stage is StrategyStage.RESEARCH and target_stage is StrategyStage.VALIDATION:
        evidence_required = True
    if evidence_required and evidence is None:
        return LifecycleEvaluation(
            EvaluationDecision.HOLD, ("MISSING_EVIDENCE",), "required evidence is unavailable"
        )
    if evidence is not None:
        if evidence.artifact_hash != artifact.artifact_hash:
            return LifecycleEvaluation(
                EvaluationDecision.REJECT,
                ("EVIDENCE_ARTIFACT_MISMATCH",),
                "evidence is bound to a different strategy artifact",
            )
        if evidence_revoked:
            return LifecycleEvaluation(
                EvaluationDecision.REJECT, ("EVIDENCE_REVOKED",), "evidence is revoked"
            )
        if evidence.oos_status is EvidenceOosStatus.CONTAMINATED:
            return LifecycleEvaluation(
                EvaluationDecision.REJECT,
                ("CONTAMINATED_OOS",),
                "contaminated OOS evidence fails closed",
            )
        if evidence.oos_status is EvidenceOosStatus.UNAVAILABLE:
            return LifecycleEvaluation(
                EvaluationDecision.HOLD, ("OOS_UNAVAILABLE",), "OOS status is unavailable"
            )
        if evidence.validation_verdict in (ResearchDecision.KILL, ResearchDecision.INVALID):
            return LifecycleEvaluation(
                EvaluationDecision.REJECT,
                ("VALIDATION_REJECTED",),
                "validation evidence has a rejecting research verdict",
            )
        if evidence_required and evidence.validation_verdict is None:
            return LifecycleEvaluation(
                EvaluationDecision.HOLD,
                ("VALIDATION_VERDICT_UNAVAILABLE",),
                "validation verdict is unavailable",
            )
        if promotion:
            if evidence.oos_status is not EvidenceOosStatus.PRISTINE:
                return LifecycleEvaluation(
                    EvaluationDecision.HOLD,
                    ("OOS_STATUS_REQUIRED",),
                    "operational eligibility requires an explicit pristine OOS status",
                )
            if evidence.deterministic_test_fixture or not evidence.empirical_eligible:
                return LifecycleEvaluation(
                    EvaluationDecision.REJECT,
                    ("FIXTURE_OR_EMPIRICAL_ELIGIBILITY_REQUIRED",),
                    "test-fixture or non-empirical evidence cannot authorize operational use",
                )
            if evidence.validation_verdict is not ResearchDecision.PASS:
                return LifecycleEvaluation(
                    EvaluationDecision.HOLD,
                    ("VALIDATION_NOT_PASSED",),
                    "operational eligibility requires an explicit passing validation verdict",
                )
    if promotion:
        if approval is None:
            return LifecycleEvaluation(
                EvaluationDecision.HOLD, ("APPROVAL_REQUIRED",), "explicit approval is required"
            )
        if approval_revoked:
            return LifecycleEvaluation(
                EvaluationDecision.REJECT, ("APPROVAL_REVOKED",), "approval is revoked"
            )
        if (
            approval.artifact_hash != artifact.artifact_hash
            or approval.evidence_id != evidence.evidence_id  # type: ignore[union-attr]
            or approval.target_stage is not target_stage
        ):
            return LifecycleEvaluation(
                EvaluationDecision.REJECT,
                ("APPROVAL_SCOPE_MISMATCH",),
                "approval does not match the exact artifact, evidence, and target stage",
            )
        if approval.actor_id == artifact.author:
            return LifecycleEvaluation(
                EvaluationDecision.REJECT,
                ("RESEARCH_SELF_APPROVAL",),
                "artifact author cannot approve its own promotion",
            )
        if state.stage is StrategyStage.VALIDATION and target_stage is StrategyStage.SHADOW:
            pass
    return LifecycleEvaluation(EvaluationDecision.ALLOW, (), "governed transition is permitted")


__all__ = [
    "ActorCapability",
    "ActorDirectory",
    "ApprovalRecord",
    "EvidenceAttachment",
    "EvidenceOosStatus",
    "EvaluationDecision",
    "FeatureDefinition",
    "LifecycleError",
    "LifecycleEvent",
    "LifecycleEvaluation",
    "LifecyclePolicy",
    "LifecycleReason",
    "RegisteredImplementation",
    "RevocationRecord",
    "ResearchReference",
    "StrategyArtifact",
    "StrategyHealth",
    "StrategyParameter",
    "StrategyStage",
    "StrategyState",
    "TrustedActor",
    "allowed_transitions",
    "evaluate_transition",
    "evaluate_current_eligibility",
]
