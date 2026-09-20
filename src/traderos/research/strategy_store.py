"""Transactional persistence for governed strategy artifacts and lifecycle state."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, insert, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.schema import Table

from traderos.database.schema import (
    metadata,
    strategy_approval_revocations,
    strategy_approvals,
    strategy_artifacts,
    strategy_evidence,
    strategy_evidence_revocations,
    strategy_lifecycle_events,
    strategy_lifecycle_state,
)
from traderos.research.strategy_lifecycle import (
    ApprovalRecord,
    EvidenceAttachment,
    LifecycleError,
    LifecycleEvent,
    LifecyclePolicy,
    StrategyArtifact,
    StrategyHealth,
    StrategyStage,
    StrategyState,
    TrustedActor,
    evaluate_transition,
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class StrategyGovernanceStore:
    """SQLAlchemy store with transaction ownership at each public mutation."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def create_schema_for_testing(self) -> None:
        """Create the aligned local schema; production uses numbered migrations."""

        try:
            metadata.create_all(self.engine)
        except SQLAlchemyError as exc:
            raise LifecycleError("unable to create strategy governance schema") from exc

    def register_artifact(
        self, artifact: StrategyArtifact, *, registered_at: datetime
    ) -> StrategyArtifact:
        existing: StrategyArtifact | None = None
        try:
            with self.engine.begin() as connection:
                row = (
                    connection.execute(
                        select(strategy_artifacts).where(
                            strategy_artifacts.c.strategy_id == artifact.strategy_id,
                            strategy_artifacts.c.strategy_version == artifact.strategy_version,
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is not None:
                    existing = self._artifact_from_row(row)
                    if existing.artifact_hash != artifact.artifact_hash:
                        raise LifecycleError("conflicting reuse of an existing strategy id/version")
                    self._ensure_state(connection, artifact, registered_at)
                    return existing
                connection.execute(
                    insert(strategy_artifacts).values(
                        artifact_hash=artifact.artifact_hash,
                        strategy_id=artifact.strategy_id,
                        strategy_version=artifact.strategy_version,
                        schema_version=artifact.schema_version,
                        author=artifact.author,
                        canonical_payload=artifact.canonical(),
                        registered_at=registered_at,
                    )
                )
                connection.execute(
                    insert(strategy_lifecycle_state).values(
                        strategy_id=artifact.strategy_id,
                        strategy_version=artifact.strategy_version,
                        artifact_hash=artifact.artifact_hash,
                        stage=StrategyStage.CANDIDATE.value,
                        health=StrategyHealth.UNKNOWN.value,
                        revision=0,
                        disabled=False,
                        disable_reason=None,
                        updated_at=registered_at,
                        last_event_id=None,
                    )
                )
        except IntegrityError:
            # A concurrent exact registration may win the unique key.  Read it
            # in a new transaction and retain loud failure for conflicting data.
            existing = self.get_artifact(artifact.strategy_id, artifact.strategy_version)
            if existing.artifact_hash != artifact.artifact_hash:
                raise LifecycleError(
                    "conflicting reuse of an existing strategy id/version"
                ) from None
            return existing
        except SQLAlchemyError as exc:
            raise LifecycleError("strategy artifact registration transaction failed") from exc
        return artifact

    def get_artifact(self, strategy_id: str, strategy_version: str) -> StrategyArtifact:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(strategy_artifacts).where(
                        strategy_artifacts.c.strategy_id == strategy_id,
                        strategy_artifacts.c.strategy_version == strategy_version,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise LifecycleError("strategy artifact does not exist")
        return self._artifact_from_row(row)

    def get_artifact_by_hash(self, artifact_hash: str) -> StrategyArtifact:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(strategy_artifacts).where(
                        strategy_artifacts.c.artifact_hash == artifact_hash
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise LifecycleError("strategy artifact does not exist")
        return self._artifact_from_row(row)

    def attach_evidence(
        self, evidence: EvidenceAttachment, *, attached_at: datetime
    ) -> EvidenceAttachment:
        try:
            with self.engine.begin() as connection:
                artifact_exists = connection.execute(
                    select(strategy_artifacts.c.artifact_hash).where(
                        strategy_artifacts.c.artifact_hash == evidence.artifact_hash
                    )
                ).scalar_one_or_none()
                if artifact_exists is None:
                    raise LifecycleError("evidence references an unknown strategy artifact")
                row = (
                    connection.execute(
                        select(strategy_evidence).where(
                            strategy_evidence.c.evidence_id == evidence.evidence_id
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is not None:
                    existing = self._evidence_from_row(row)
                    if existing.evidence_hash != evidence.evidence_hash:
                        raise LifecycleError("conflicting reuse of evidence identity")
                    return existing
                connection.execute(
                    insert(strategy_evidence).values(
                        evidence_id=evidence.evidence_id,
                        evidence_hash=evidence.evidence_hash,
                        artifact_hash=evidence.artifact_hash,
                        experiment_id=evidence.experiment_id,
                        result_hash=evidence.result_hash,
                        canonical_payload=evidence.canonical(),
                        attached_at=attached_at,
                    )
                )
        except IntegrityError as exc:
            raise LifecycleError("conflicting immutable evidence registration") from exc
        except SQLAlchemyError as exc:
            raise LifecycleError("evidence attachment transaction failed") from exc
        return evidence

    def get_evidence(self, evidence_id: str) -> EvidenceAttachment:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(strategy_evidence).where(strategy_evidence.c.evidence_id == evidence_id)
                )
                .mappings()
                .first()
            )
        if row is None:
            raise LifecycleError("strategy evidence does not exist")
        return self._evidence_from_row(row)

    def list_evidence(self, artifact_hash: str) -> tuple[EvidenceAttachment, ...]:
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(strategy_evidence)
                    .where(strategy_evidence.c.artifact_hash == artifact_hash)
                    .order_by(
                        strategy_evidence.c.attached_at.desc(),
                        strategy_evidence.c.evidence_id.desc(),
                    )
                )
                .mappings()
                .all()
            )
        return tuple(self._evidence_from_row(row) for row in rows)

    def record_approval(self, approval: ApprovalRecord) -> ApprovalRecord:
        try:
            with self.engine.begin() as connection:
                artifact_exists = connection.execute(
                    select(strategy_artifacts.c.artifact_hash).where(
                        strategy_artifacts.c.artifact_hash == approval.artifact_hash
                    )
                ).scalar_one_or_none()
                evidence = (
                    connection.execute(
                        select(strategy_evidence).where(
                            strategy_evidence.c.evidence_id == approval.evidence_id
                        )
                    )
                    .mappings()
                    .first()
                )
                if artifact_exists is None or evidence is None:
                    raise LifecycleError("approval references unknown artifact or evidence")
                if evidence["artifact_hash"] != approval.artifact_hash:
                    raise LifecycleError("approval evidence is bound to a different artifact")
                row = (
                    connection.execute(
                        select(strategy_approvals).where(
                            strategy_approvals.c.approval_id == approval.approval_id
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is not None:
                    existing = self._approval_from_row(row)
                    if existing.approval_hash != approval.approval_hash:
                        raise LifecycleError("conflicting reuse of approval identity")
                    return existing
                connection.execute(
                    insert(strategy_approvals).values(
                        approval_id=approval.approval_id,
                        approval_hash=approval.approval_hash,
                        artifact_hash=approval.artifact_hash,
                        evidence_id=approval.evidence_id,
                        target_stage=approval.target_stage.value,
                        actor_id=approval.actor_id,
                        policy_id=approval.policy_id,
                        policy_version=approval.policy_version,
                        canonical_payload=approval.canonical(),
                        approved_at=approval.approved_at,
                    )
                )
        except IntegrityError as exc:
            raise LifecycleError("conflicting immutable approval registration") from exc
        except SQLAlchemyError as exc:
            raise LifecycleError("approval registration transaction failed") from exc
        return approval

    def get_approval(self, approval_id: str) -> ApprovalRecord:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(strategy_approvals).where(
                        strategy_approvals.c.approval_id == approval_id
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise LifecycleError("strategy approval does not exist")
        return self._approval_from_row(row)

    def list_approvals(
        self, artifact_hash: str, evidence_id: str | None = None
    ) -> tuple[ApprovalRecord, ...]:
        statement = select(strategy_approvals).where(
            strategy_approvals.c.artifact_hash == artifact_hash
        )
        if evidence_id is not None:
            statement = statement.where(strategy_approvals.c.evidence_id == evidence_id)
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    statement.order_by(
                        strategy_approvals.c.approved_at.desc(),
                        strategy_approvals.c.approval_id.desc(),
                    )
                )
                .mappings()
                .all()
            )
        return tuple(self._approval_from_row(row) for row in rows)

    def revoke_evidence(
        self,
        *,
        evidence_id: str,
        actor_id: str,
        reason: str,
        idempotency_key: str,
        revoked_at: datetime,
    ) -> str:
        return self._revoke(
            strategy_evidence_revocations,
            "evidence_id",
            evidence_id,
            actor_id,
            reason,
            idempotency_key,
            revoked_at,
        )

    def revoke_approval(
        self,
        *,
        approval_id: str,
        actor_id: str,
        reason: str,
        idempotency_key: str,
        revoked_at: datetime,
    ) -> str:
        return self._revoke(
            strategy_approval_revocations,
            "approval_id",
            approval_id,
            actor_id,
            reason,
            idempotency_key,
            revoked_at,
        )

    def is_evidence_revoked(
        self, evidence_id: str, *, connection: Connection | None = None
    ) -> bool:
        return self._is_revoked(
            strategy_evidence_revocations, "evidence_id", evidence_id, connection
        )

    def is_approval_revoked(
        self, approval_id: str, *, connection: Connection | None = None
    ) -> bool:
        return self._is_revoked(
            strategy_approval_revocations, "approval_id", approval_id, connection
        )

    def get_state(self, strategy_id: str, strategy_version: str) -> StrategyState:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(strategy_lifecycle_state).where(
                        strategy_lifecycle_state.c.strategy_id == strategy_id,
                        strategy_lifecycle_state.c.strategy_version == strategy_version,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise LifecycleError("strategy lifecycle state does not exist")
        state = self._state_from_row(row)
        artifact = self.get_artifact_by_hash(state.artifact_hash)
        if (
            artifact.strategy_id != state.strategy_id
            or artifact.strategy_version != state.strategy_version
        ):
            raise LifecycleError(
                "strategy lifecycle state is bound to a different artifact identity"
            )
        return state

    def transition(
        self,
        *,
        state: StrategyState,
        actor: TrustedActor,
        policy: LifecyclePolicy,
        target_stage: StrategyStage,
        actor_id: str,
        policy_id: str,
        policy_version: str,
        evidence_id: str | None,
        approval_id: str | None,
        reason_code: str,
        reason: str,
        idempotency_key: str,
        correlation_id: str,
        occurred_at: datetime,
    ) -> LifecycleEvent:
        try:
            with self.engine.begin() as connection:
                current_row = (
                    connection.execute(
                        select(strategy_lifecycle_state)
                        .where(
                            strategy_lifecycle_state.c.strategy_id == state.strategy_id,
                            strategy_lifecycle_state.c.strategy_version == state.strategy_version,
                        )
                        .with_for_update()
                    )
                    .mappings()
                    .first()
                )
                if current_row is None:
                    raise LifecycleError("strategy lifecycle state does not exist")
                existing_row = (
                    connection.execute(
                        select(strategy_lifecycle_events).where(
                            strategy_lifecycle_events.c.strategy_id == state.strategy_id,
                            strategy_lifecycle_events.c.strategy_version == state.strategy_version,
                            strategy_lifecycle_events.c.idempotency_key == idempotency_key,
                        )
                    )
                    .mappings()
                    .first()
                )
                if existing_row is not None:
                    existing = self._event_from_row(existing_row)
                    if not self._same_request(
                        existing,
                        target_stage,
                        actor_id,
                        policy_id,
                        policy_version,
                        evidence_id,
                        approval_id,
                        reason_code,
                        reason,
                        state.revision,
                    ):
                        raise LifecycleError(
                            "idempotency key conflicts with a prior lifecycle event"
                        )
                    return existing
                current = self._state_from_row(current_row)
                if current.revision != state.revision:
                    raise LifecycleError("lifecycle transition uses a superseded state revision")
                artifact = self._artifact_from_connection(connection, current.artifact_hash)
                evidence = (
                    self._evidence_from_connection(connection, evidence_id)
                    if evidence_id is not None
                    else None
                )
                approval = (
                    self._approval_from_connection(connection, approval_id)
                    if approval_id is not None
                    else None
                )
                evaluation = evaluate_transition(
                    state=current,
                    artifact=artifact,
                    target_stage=target_stage,
                    actor=actor,
                    policy=policy,
                    evidence=evidence,
                    approval=approval,
                    evidence_revoked=(
                        self.is_evidence_revoked(evidence_id, connection=connection)
                        if evidence_id is not None
                        else False
                    ),
                    approval_revoked=(
                        self.is_approval_revoked(approval_id, connection=connection)
                        if approval_id is not None
                        else False
                    ),
                    reason=reason,
                )
                if evaluation.decision.value != "allow":
                    raise LifecycleError(
                        f"lifecycle transition {evaluation.decision.value}: "
                        f"{','.join(evaluation.reason_codes)}"
                    )
                new_revision = current.revision + 1
                event = LifecycleEvent(
                    event_id=f"strategy-event-{uuid4().hex}",
                    strategy_id=current.strategy_id,
                    strategy_version=current.strategy_version,
                    artifact_hash=current.artifact_hash,
                    event_type="transition",
                    previous_stage=current.stage,
                    resulting_stage=target_stage,
                    expected_revision=current.revision,
                    resulting_revision=new_revision,
                    actor_id=actor_id,
                    policy_id=policy_id,
                    policy_version=policy_version,
                    evidence_id=evidence_id,
                    approval_id=approval_id,
                    reason_code=reason_code,
                    reason=reason,
                    occurred_at=occurred_at,
                    idempotency_key=idempotency_key,
                    correlation_id=correlation_id,
                )
                self._insert_event(connection, event)
                self._update_state(
                    connection,
                    current,
                    event,
                    disabled=current.disabled,
                    disable_reason=current.disable_reason,
                )
                return event
        except LifecycleError:
            raise
        except SQLAlchemyError as exc:
            raise LifecycleError("lifecycle transition transaction failed") from exc

    def disable(
        self,
        *,
        state: StrategyState,
        actor_id: str,
        policy_id: str,
        policy_version: str,
        reason: str,
        idempotency_key: str,
        correlation_id: str,
        occurred_at: datetime,
    ) -> LifecycleEvent:
        try:
            with self.engine.begin() as connection:
                current_row = (
                    connection.execute(
                        select(strategy_lifecycle_state)
                        .where(
                            strategy_lifecycle_state.c.strategy_id == state.strategy_id,
                            strategy_lifecycle_state.c.strategy_version == state.strategy_version,
                        )
                        .with_for_update()
                    )
                    .mappings()
                    .first()
                )
                if current_row is None:
                    raise LifecycleError("strategy lifecycle state does not exist")
                existing_row = (
                    connection.execute(
                        select(strategy_lifecycle_events).where(
                            strategy_lifecycle_events.c.strategy_id == state.strategy_id,
                            strategy_lifecycle_events.c.strategy_version == state.strategy_version,
                            strategy_lifecycle_events.c.idempotency_key == idempotency_key,
                        )
                    )
                    .mappings()
                    .first()
                )
                if existing_row is not None:
                    existing = self._event_from_row(existing_row)
                    if (
                        existing.event_type != "disable"
                        or existing.reason != reason
                        or existing.actor_id != actor_id
                    ):
                        raise LifecycleError(
                            "idempotency key conflicts with a prior lifecycle event"
                        )
                    return existing
                current = self._state_from_row(current_row)
                if current.revision != state.revision:
                    raise LifecycleError("disablement uses a superseded state revision")
                event = LifecycleEvent(
                    event_id=f"strategy-event-{uuid4().hex}",
                    strategy_id=current.strategy_id,
                    strategy_version=current.strategy_version,
                    artifact_hash=current.artifact_hash,
                    event_type="disable",
                    previous_stage=current.stage,
                    resulting_stage=current.stage,
                    expected_revision=current.revision,
                    resulting_revision=current.revision + 1,
                    actor_id=actor_id,
                    policy_id=policy_id,
                    policy_version=policy_version,
                    evidence_id=None,
                    approval_id=None,
                    reason_code="STRATEGY_DISABLED",
                    reason=reason,
                    occurred_at=occurred_at,
                    idempotency_key=idempotency_key,
                    correlation_id=correlation_id,
                )
                self._insert_event(connection, event)
                self._update_state(connection, current, event, disabled=True, disable_reason=reason)
                return event
        except LifecycleError:
            raise
        except SQLAlchemyError as exc:
            raise LifecycleError("strategy disablement transaction failed") from exc

    def list_history(self, strategy_id: str, strategy_version: str) -> tuple[LifecycleEvent, ...]:
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(strategy_lifecycle_events)
                    .where(
                        strategy_lifecycle_events.c.strategy_id == strategy_id,
                        strategy_lifecycle_events.c.strategy_version == strategy_version,
                    )
                    .order_by(strategy_lifecycle_events.c.resulting_revision)
                )
                .mappings()
                .all()
            )
        return tuple(self._event_from_row(row) for row in rows)

    def reconcile(self, strategy_id: str, strategy_version: str) -> StrategyState:
        state = self.get_state(strategy_id, strategy_version)
        self.get_artifact_by_hash(state.artifact_hash)
        history = self.list_history(strategy_id, strategy_version)
        revision = 0
        stage = StrategyStage.CANDIDATE
        last_event_id: str | None = None
        for event in history:
            if (
                event.artifact_hash != state.artifact_hash
                or event.expected_revision != revision
                or event.resulting_revision != revision + 1
                or event.previous_stage is not stage
            ):
                raise LifecycleError(
                    "strategy lifecycle history cannot reconcile with its projection"
                )
            revision = event.resulting_revision
            stage = event.resulting_stage
            last_event_id = event.event_id
        if (
            state.revision != revision
            or state.stage is not stage
            or state.last_event_id != last_event_id
        ):
            raise LifecycleError(
                "strategy lifecycle projection is inconsistent with append-only history"
            )
        return state

    def _ensure_state(
        self, connection: Connection, artifact: StrategyArtifact, timestamp: datetime
    ) -> None:
        row = (
            connection.execute(
                select(strategy_lifecycle_state).where(
                    strategy_lifecycle_state.c.strategy_id == artifact.strategy_id,
                    strategy_lifecycle_state.c.strategy_version == artifact.strategy_version,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            connection.execute(
                insert(strategy_lifecycle_state).values(
                    strategy_id=artifact.strategy_id,
                    strategy_version=artifact.strategy_version,
                    artifact_hash=artifact.artifact_hash,
                    stage=StrategyStage.CANDIDATE.value,
                    health=StrategyHealth.UNKNOWN.value,
                    revision=0,
                    disabled=False,
                    disable_reason=None,
                    updated_at=timestamp,
                    last_event_id=None,
                )
            )

    @staticmethod
    def _insert_event(connection: Connection, event: LifecycleEvent) -> None:
        connection.execute(
            insert(strategy_lifecycle_events).values(
                event_id=event.event_id,
                strategy_id=event.strategy_id,
                strategy_version=event.strategy_version,
                artifact_hash=event.artifact_hash,
                event_type=event.event_type,
                previous_stage=event.previous_stage.value,
                resulting_stage=event.resulting_stage.value,
                expected_revision=event.expected_revision,
                resulting_revision=event.resulting_revision,
                actor_id=event.actor_id,
                policy_id=event.policy_id,
                policy_version=event.policy_version,
                evidence_id=event.evidence_id,
                approval_id=event.approval_id,
                reason_code=event.reason_code,
                reason=event.reason,
                occurred_at=event.occurred_at,
                idempotency_key=event.idempotency_key,
                correlation_id=event.correlation_id,
            )
        )

    @staticmethod
    def _update_state(
        connection: Connection,
        current: StrategyState,
        event: LifecycleEvent,
        *,
        disabled: bool,
        disable_reason: str | None,
    ) -> None:
        result = connection.execute(
            update(strategy_lifecycle_state)
            .where(
                strategy_lifecycle_state.c.strategy_id == current.strategy_id,
                strategy_lifecycle_state.c.strategy_version == current.strategy_version,
                strategy_lifecycle_state.c.revision == event.expected_revision,
            )
            .values(
                stage=event.resulting_stage.value,
                revision=event.resulting_revision,
                disabled=disabled,
                disable_reason=disable_reason,
                updated_at=event.occurred_at,
                last_event_id=event.event_id,
            )
        )
        if result.rowcount != 1:
            raise LifecycleError("lifecycle projection revision update failed")

    @staticmethod
    def _same_request(
        event: LifecycleEvent,
        target_stage: StrategyStage,
        actor_id: str,
        policy_id: str,
        policy_version: str,
        evidence_id: str | None,
        approval_id: str | None,
        reason_code: str,
        reason: str,
        expected_revision: int,
    ) -> bool:
        return (
            event.event_type == "transition"
            and event.resulting_stage is target_stage
            and event.actor_id == actor_id
            and event.policy_id == policy_id
            and event.policy_version == policy_version
            and event.evidence_id == evidence_id
            and event.approval_id == approval_id
            and event.reason_code == reason_code
            and event.reason == reason
            and event.expected_revision == expected_revision
        )

    @staticmethod
    def _artifact_from_row(row: Any) -> StrategyArtifact:
        return StrategyArtifact.from_canonical(
            row["canonical_payload"], expected_hash=row["artifact_hash"]
        )

    @staticmethod
    def _evidence_from_row(row: Any) -> EvidenceAttachment:
        return EvidenceAttachment.from_canonical(
            row["canonical_payload"], expected_hash=row["evidence_hash"]
        )

    @staticmethod
    def _approval_from_row(row: Any) -> ApprovalRecord:
        approval = ApprovalRecord.from_canonical(row["canonical_payload"])
        if approval.approval_hash != row["approval_hash"]:
            raise LifecycleError("strategy approval hash mismatch")
        return approval

    @staticmethod
    def _state_from_row(row: Any) -> StrategyState:
        return StrategyState(
            strategy_id=row["strategy_id"],
            strategy_version=row["strategy_version"],
            artifact_hash=row["artifact_hash"],
            stage=StrategyStage(row["stage"]),
            health=StrategyHealth(row["health"]),
            revision=row["revision"],
            disabled=row["disabled"],
            disable_reason=row["disable_reason"],
            updated_at=_utc(row["updated_at"]),
            last_event_id=row["last_event_id"],
        )

    @staticmethod
    def _event_from_row(row: Any) -> LifecycleEvent:
        return LifecycleEvent(
            event_id=row["event_id"],
            strategy_id=row["strategy_id"],
            strategy_version=row["strategy_version"],
            artifact_hash=row["artifact_hash"],
            event_type=row["event_type"],
            previous_stage=StrategyStage(row["previous_stage"]),
            resulting_stage=StrategyStage(row["resulting_stage"]),
            expected_revision=row["expected_revision"],
            resulting_revision=row["resulting_revision"],
            actor_id=row["actor_id"],
            policy_id=row["policy_id"],
            policy_version=row["policy_version"],
            evidence_id=row["evidence_id"],
            approval_id=row["approval_id"],
            reason_code=row["reason_code"],
            reason=row["reason"],
            occurred_at=_utc(row["occurred_at"]),
            idempotency_key=row["idempotency_key"],
            correlation_id=row["correlation_id"],
        )

    @staticmethod
    def _artifact_from_connection(connection: Connection, artifact_hash: str) -> StrategyArtifact:
        row = (
            connection.execute(
                select(strategy_artifacts).where(
                    strategy_artifacts.c.artifact_hash == artifact_hash
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise LifecycleError("strategy artifact does not exist")
        return StrategyGovernanceStore._artifact_from_row(row)

    @staticmethod
    def _evidence_from_connection(connection: Connection, evidence_id: str) -> EvidenceAttachment:
        row = (
            connection.execute(
                select(strategy_evidence).where(strategy_evidence.c.evidence_id == evidence_id)
            )
            .mappings()
            .first()
        )
        if row is None:
            raise LifecycleError("strategy evidence does not exist")
        return StrategyGovernanceStore._evidence_from_row(row)

    @staticmethod
    def _approval_from_connection(connection: Connection, approval_id: str) -> ApprovalRecord:
        row = (
            connection.execute(
                select(strategy_approvals).where(strategy_approvals.c.approval_id == approval_id)
            )
            .mappings()
            .first()
        )
        if row is None:
            raise LifecycleError("strategy approval does not exist")
        return StrategyGovernanceStore._approval_from_row(row)

    def _revoke(
        self,
        table: Table,
        target_column: str,
        target_id: str,
        actor_id: str,
        reason: str,
        idempotency_key: str,
        revoked_at: datetime,
    ) -> str:
        if not reason.strip() or not actor_id.strip() or not idempotency_key.strip():
            raise LifecycleError("revocation actor, reason, and idempotency key are required")
        revocation_id = f"strategy-revocation-{uuid4().hex}"
        try:
            with self.engine.begin() as connection:
                existing = (
                    connection.execute(
                        select(table).where(table.c.idempotency_key == idempotency_key)
                    )
                    .mappings()
                    .first()
                )
                if existing is not None:
                    if (
                        existing[target_column] != target_id
                        or existing["actor_id"] != actor_id
                        or existing["reason"] != reason
                    ):
                        raise LifecycleError("idempotency key conflicts with a prior revocation")
                    return str(existing["revocation_id"])
                target = connection.execute(
                    select(
                        (
                            strategy_evidence
                            if target_column == "evidence_id"
                            else strategy_approvals
                        ).c[target_column]
                    ).where(
                        (
                            strategy_evidence
                            if target_column == "evidence_id"
                            else strategy_approvals
                        ).c[target_column]
                        == target_id
                    )
                ).first()
                if target is None:
                    raise LifecycleError("revocation target does not exist")
                connection.execute(
                    insert(table).values(
                        revocation_id=revocation_id,
                        **{
                            target_column: target_id,
                            "actor_id": actor_id,
                            "reason": reason,
                            "revoked_at": revoked_at,
                            "idempotency_key": idempotency_key,
                        },
                    )
                )
        except LifecycleError:
            raise
        except IntegrityError as exc:
            raise LifecycleError("conflicting revocation registration") from exc
        except SQLAlchemyError as exc:
            raise LifecycleError("revocation transaction failed") from exc
        return revocation_id

    def _is_revoked(
        self, table: Table, target_column: str, target_id: str, connection: Connection | None
    ) -> bool:
        if connection is None:
            with self.engine.connect() as owned:
                return (
                    owned.execute(
                        select(table.c.revocation_id).where(table.c[target_column] == target_id)
                    ).first()
                    is not None
                )
        return (
            connection.execute(
                select(table.c.revocation_id).where(table.c[target_column] == target_id)
            ).first()
            is not None
        )


__all__ = ["StrategyGovernanceStore"]
