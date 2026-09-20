"""Internal application service for versioned strategy governance.

The service resolves trusted actors, validates cross-registry references, and
delegates all state changes to the transactional governance store.  It has no
paper-order or broker dependency by design.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from traderos.data.time import require_utc
from traderos.research.registry import ExperimentRegistry
from traderos.research.strategy_lifecycle import (
    ActorCapability,
    ActorDirectory,
    ApprovalRecord,
    EvaluationDecision,
    EvidenceAttachment,
    LifecycleError,
    LifecycleEvaluation,
    LifecycleEvent,
    LifecyclePolicy,
    LifecycleReason,
    StrategyArtifact,
    StrategyStage,
    StrategyState,
    evaluate_current_eligibility,
    evaluate_transition,
)
from traderos.research.strategy_store import StrategyGovernanceStore


@dataclass(frozen=True)
class EligibilityExplanation:
    strategy_id: str
    strategy_version: str
    artifact_hash: str
    stage: StrategyStage
    disabled: bool
    operationally_eligible: bool
    reasons: tuple[str, ...]
    evidence_id: str | None
    approval_id: str | None


class StrategyGovernanceService:
    """Minimal internal boundary for strategy artifact and lifecycle mutations."""

    _DEFAULT_IMPLEMENTATIONS = frozenset(
        {("always_flat", "1"), ("buy_and_hold", "1"), ("fixed_orders", "1")}
    )

    def __init__(
        self,
        store: StrategyGovernanceStore,
        actors: ActorDirectory,
        *,
        experiment_registry: ExperimentRegistry | None = None,
        registered_implementations: Sequence[tuple[str, str]] | None = None,
        policy: LifecyclePolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.actors = actors
        self.experiment_registry = experiment_registry
        self.registered_implementations = frozenset(
            registered_implementations
            if registered_implementations is not None
            else self._DEFAULT_IMPLEMENTATIONS
        )
        self.policy = policy or LifecyclePolicy("strategy-governance", "1")
        self._clock = clock or (lambda: datetime.now(UTC))

    def register_strategy_artifact(
        self, artifact: StrategyArtifact, *, actor_id: str
    ) -> StrategyArtifact:
        actor = self.actors.resolve(actor_id)
        if not actor.can(ActorCapability.RESEARCH):
            raise LifecycleError("actor is not authorized to register research artifacts")
        if (
            artifact.implementation.implementation_id,
            artifact.implementation.implementation_version,
        ) not in self.registered_implementations:
            raise LifecycleError(
                "strategy implementation is not in the registered implementation catalog"
            )
        self._verify_artifact_references(artifact)
        return self.store.register_artifact(artifact, registered_at=self._now())

    def get_strategy_artifact(self, strategy_id: str, strategy_version: str) -> StrategyArtifact:
        return self.store.get_artifact(strategy_id, strategy_version)

    def attach_evidence(self, evidence: EvidenceAttachment, *, actor_id: str) -> EvidenceAttachment:
        actor = self.actors.resolve(actor_id)
        if not actor.can(ActorCapability.RESEARCH):
            raise LifecycleError("actor is not authorized to attach research evidence")
        artifact = self.store.get_artifact_by_hash(evidence.artifact_hash)
        self._verify_experiment_reference(artifact, evidence)
        return self.store.attach_evidence(evidence, attached_at=self._now())

    def evaluate_transition(
        self,
        strategy_id: str,
        strategy_version: str,
        *,
        target_stage: StrategyStage,
        actor_id: str,
        evidence_id: str | None = None,
        approval_id: str | None = None,
        reason: str,
    ) -> LifecycleEvaluation:
        state = self.store.get_state(strategy_id, strategy_version)
        artifact = self.store.get_artifact_by_hash(state.artifact_hash)
        evidence = self.store.get_evidence(evidence_id) if evidence_id is not None else None
        approval = self.store.get_approval(approval_id) if approval_id is not None else None
        return evaluate_transition(
            state=state,
            artifact=artifact,
            target_stage=target_stage,
            actor=self.actors.resolve(actor_id),
            policy=self.policy,
            evidence=evidence,
            approval=approval,
            evidence_revoked=(
                self.store.is_evidence_revoked(evidence_id) if evidence_id is not None else False
            ),
            approval_revoked=(
                self.store.is_approval_revoked(approval_id) if approval_id is not None else False
            ),
            reason=reason,
        )

    def request_or_record_approval(
        self,
        *,
        approval: ApprovalRecord,
        actor_id: str,
    ) -> ApprovalRecord:
        actor = self.actors.resolve(actor_id)
        if not actor.can(ActorCapability.APPROVE):
            raise LifecycleError("actor is not authorized to issue lifecycle approvals")
        if actor.actor_id != approval.actor_id:
            raise LifecycleError("approval actor must be resolved by the trusted actor boundary")
        artifact = self.store.get_artifact_by_hash(approval.artifact_hash)
        if approval.actor_id == artifact.author:
            raise LifecycleError("research roles cannot approve their own strategy artifact")
        evidence = self.store.get_evidence(approval.evidence_id)
        if evidence.artifact_hash != artifact.artifact_hash:
            raise LifecycleError("approval evidence is bound to a different strategy artifact")
        if self.store.is_evidence_revoked(evidence.evidence_id):
            raise LifecycleError("revoked evidence cannot receive a new approval")
        return self.store.record_approval(approval)

    def transition_strategy(
        self,
        strategy_id: str,
        strategy_version: str,
        *,
        target_stage: StrategyStage,
        actor_id: str,
        expected_revision: int,
        idempotency_key: str,
        reason: str,
        evidence_id: str | None = None,
        approval_id: str | None = None,
        correlation_id: str | None = None,
    ) -> StrategyState:
        state = self.store.get_state(strategy_id, strategy_version)
        existing = self._event_for_key(strategy_id, strategy_version, idempotency_key)
        expected_reason_code = self._reason_code(state.stage, target_stage)
        if existing is not None:
            if (
                existing.event_type == "transition"
                and existing.resulting_stage is target_stage
                and existing.actor_id == actor_id
                and existing.evidence_id == evidence_id
                and existing.approval_id == approval_id
                and existing.reason_code == expected_reason_code
                and existing.reason == reason
                and existing.expected_revision == expected_revision
            ):
                return state
            raise LifecycleError("idempotency key conflicts with a prior lifecycle event")
        if expected_revision != state.revision:
            raise LifecycleError("lifecycle transition uses a superseded state revision")
        evaluation = self.evaluate_transition(
            strategy_id,
            strategy_version,
            target_stage=target_stage,
            actor_id=actor_id,
            evidence_id=evidence_id,
            approval_id=approval_id,
            reason=reason,
        )
        if evaluation.decision is not EvaluationDecision.ALLOW:
            raise LifecycleError(
                f"lifecycle transition {evaluation.decision.value}: "
                f"{','.join(evaluation.reason_codes)}"
            )
        actor = self.actors.resolve(actor_id)
        self.store.transition(
            state=state,
            actor=actor,
            policy=self.policy,
            target_stage=target_stage,
            actor_id=actor_id,
            policy_id=self.policy.policy_id,
            policy_version=self.policy.version,
            evidence_id=evidence_id,
            approval_id=approval_id,
            reason_code=self._reason_code(state.stage, target_stage),
            reason=reason,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id or f"strategy-trace-{uuid4().hex}",
            occurred_at=self._now(),
        )
        return self.store.get_state(strategy_id, strategy_version)

    def disable_strategy(
        self,
        strategy_id: str,
        strategy_version: str,
        *,
        actor_id: str,
        expected_revision: int,
        idempotency_key: str,
        reason: str,
        correlation_id: str | None = None,
    ) -> StrategyState:
        actor = self.actors.resolve(actor_id)
        if not actor.can(ActorCapability.DISABLE):
            raise LifecycleError("actor is not authorized to disable a strategy version")
        state = self.store.get_state(strategy_id, strategy_version)
        existing = self._event_for_key(strategy_id, strategy_version, idempotency_key)
        if existing is not None:
            if (
                existing.event_type == "disable"
                and existing.actor_id == actor_id
                and existing.reason == reason
                and existing.expected_revision == expected_revision
            ):
                return state
            raise LifecycleError("idempotency key conflicts with a prior lifecycle event")
        if expected_revision != state.revision:
            raise LifecycleError("disablement uses a superseded state revision")
        if not reason.strip():
            raise LifecycleError("strategy disablement requires a reason")
        self.store.disable(
            state=state,
            actor_id=actor_id,
            policy_id=self.policy.policy_id,
            policy_version=self.policy.version,
            reason=reason,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id or f"strategy-trace-{uuid4().hex}",
            occurred_at=self._now(),
        )
        return self.store.get_state(strategy_id, strategy_version)

    def get_strategy_state(self, strategy_id: str, strategy_version: str) -> StrategyState:
        return self.store.get_state(strategy_id, strategy_version)

    def list_transition_history(
        self, strategy_id: str, strategy_version: str
    ) -> tuple[object, ...]:
        return self.store.list_history(strategy_id, strategy_version)

    def explain_strategy_eligibility(
        self, strategy_id: str, strategy_version: str
    ) -> EligibilityExplanation:
        state = self.store.get_state(strategy_id, strategy_version)
        artifact = self.store.get_artifact_by_hash(state.artifact_hash)
        evidence = self._best_evidence(state.artifact_hash)
        approval = self._best_approval(state.artifact_hash, evidence)
        evaluation = evaluate_current_eligibility(
            state=state,
            artifact=artifact,
            evidence=evidence,
            approval=approval,
            evidence_revoked=(
                self.store.is_evidence_revoked(evidence.evidence_id)
                if evidence is not None
                else False
            ),
            approval_revoked=(
                self.store.is_approval_revoked(approval.approval_id)
                if approval is not None
                else False
            ),
        )
        return EligibilityExplanation(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            artifact_hash=state.artifact_hash,
            stage=state.stage,
            disabled=state.disabled,
            operationally_eligible=evaluation.decision is EvaluationDecision.ALLOW,
            reasons=evaluation.reason_codes,
            evidence_id=evidence.evidence_id if evidence is not None else None,
            approval_id=approval.approval_id if approval is not None else None,
        )

    def reconcile_strategy(self, strategy_id: str, strategy_version: str) -> StrategyState:
        return self.store.reconcile(strategy_id, strategy_version)

    def _verify_experiment_reference(
        self, artifact: StrategyArtifact, evidence: EvidenceAttachment
    ) -> None:
        if self.experiment_registry is None:
            raise LifecycleError("an experiment registry is required to attach evidence")
        payload = self.experiment_registry.read(evidence.experiment_id)
        result = payload.get("result")
        experiment = payload.get("experiment")
        if not isinstance(result, dict) or result.get("result_hash") != evidence.result_hash:
            raise LifecycleError("evidence result reference does not verify")
        if not isinstance(experiment, dict):
            raise LifecycleError("evidence experiment reference is malformed")
        dataset_hash = experiment.get("dataset_hash")
        if dataset_hash not in evidence.dataset_hashes:
            raise LifecycleError("evidence dataset identity does not match its experiment")
        if (
            artifact.dataset_identity is not None
            and artifact.dataset_identity not in evidence.dataset_hashes
        ):
            raise LifecycleError("evidence does not cover the artifact dataset identity")
        locked_oos_touched = result.get("locked_oos_touched")
        contaminated = result.get("selection_influenced_by_locked_oos")
        if evidence.oos_status is not None and (
            evidence.oos_status.value == "pristine"
            and (locked_oos_touched is not True or contaminated is not False)
        ):
            raise LifecycleError("pristine OOS evidence is not supported by the immutable result")
        if evidence.oos_status.value == "contaminated" and contaminated is not True:
            raise LifecycleError("contaminated OOS evidence does not match the immutable result")

    def _verify_artifact_references(self, artifact: StrategyArtifact) -> None:
        references = tuple(
            reference
            for reference in (
                artifact.training_reference,
                artifact.validation_reference,
                artifact.oos_reference,
            )
            if reference is not None
        )
        if not references:
            return
        if self.experiment_registry is None:
            raise LifecycleError("an experiment registry is required for artifact references")
        for reference in references:
            payload = self.experiment_registry.read(reference.reference_id)
            experiment = payload.get("experiment")
            if not isinstance(experiment, dict):
                raise LifecycleError("strategy artifact experiment reference is malformed")
            if (
                experiment.get("dataset_hash") != reference.dataset_hash
                or experiment.get("scope") != reference.scope.value
            ):
                raise LifecycleError("strategy artifact research reference does not verify")

    def _best_evidence(self, artifact_hash: str) -> EvidenceAttachment | None:
        values = self.store.list_evidence(artifact_hash)
        return values[0] if values else None

    def _event_for_key(
        self, strategy_id: str, strategy_version: str, key: str
    ) -> LifecycleEvent | None:
        return next(
            (
                event
                for event in self.store.list_history(strategy_id, strategy_version)
                if event.idempotency_key == key
            ),
            None,
        )

    def _best_approval(
        self, artifact_hash: str, evidence: EvidenceAttachment | None
    ) -> ApprovalRecord | None:
        if evidence is None:
            return None
        values = self.store.list_approvals(artifact_hash, evidence.evidence_id)
        return values[0] if values else None

    def _now(self) -> datetime:
        value = self._clock()
        require_utc(value)
        return value

    @staticmethod
    def _reason_code(source: StrategyStage, target: StrategyStage) -> str:
        if target is StrategyStage.RETIRED:
            return LifecycleReason.RETIRED.value
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
        if stage_order[target] < stage_order[source]:
            return LifecycleReason.GOVERNED_DEMOTION.value
        return {
            StrategyStage.RESEARCH: LifecycleReason.RESEARCH_STARTED.value,
            StrategyStage.VALIDATION: LifecycleReason.VALIDATION_RECORDED.value,
            StrategyStage.SHADOW: LifecycleReason.EXPLICIT_APPROVAL.value,
            StrategyStage.PAPER: LifecycleReason.EXPLICIT_APPROVAL.value,
        }.get(target, LifecycleReason.GOVERNED_DEMOTION.value)


__all__ = ["EligibilityExplanation", "StrategyGovernanceService"]
