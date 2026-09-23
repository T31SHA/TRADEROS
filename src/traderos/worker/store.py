"""Durable state and single-worker coordination for the paper supervisor."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import Engine, delete, inspect, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError, OperationalError

from traderos.data.time import require_utc
from traderos.database.schema import metadata, worker_cycles, worker_leases, worker_states
from traderos.execution.authority import ExecutionAuthority
from traderos.worker.models import (
    DEFAULT_STRATEGY_HEALTH_CYCLE_LIMIT,
    CycleOutcome,
    DecisionTraceEvent,
    StrategyHealthMetric,
    StrategyHealthReport,
    WorkerHealth,
    WorkerState,
    WorkerStatus,
)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return value
    if value.tzinfo is None or value.utcoffset() is None:
        # SQLite-backed legacy rows may be naive; those rows were written by
        # this store's UTC-only boundary and are interpreted as UTC on read.
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class WorkerStore:
    """Own worker status, progress, and workload lease persistence."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @contextmanager
    def _transaction(self, *, immediate_sqlite: bool = False) -> Iterator[Connection]:
        """Open a coordination transaction with SQLite's write lock when needed."""

        if immediate_sqlite and self.engine.dialect.name == "sqlite":
            with self.engine.connect() as connection:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                try:
                    yield connection
                except BaseException:
                    connection.rollback()
                    raise
                else:
                    connection.commit()
            return
        with self.engine.begin() as connection:
            yield connection

    def create_schema(self) -> None:
        if self.engine.dialect.name == "postgresql":
            self._require_migrated_schema()
            return
        if self.engine.dialect.name == "sqlite":
            # ``create_all`` performs a check-then-create sequence.  Status
            # probes and worker processes can start concurrently, so serialize
            # that sequence behind SQLite's writer lock instead of allowing
            # two processes to both observe a missing table.
            with self.engine.connect() as connection:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                try:
                    metadata.create_all(connection)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        else:
            metadata.create_all(self.engine)
        self._ensure_lease_claim_column()
        self._ensure_lease_generation_column()
        self._ensure_paper_authorization_columns()
        self._ensure_data_latest_column()

    def _require_migrated_schema(self) -> None:
        """Reject PostgreSQL startup when numbered migrations are incomplete."""

        inspector = inspect(self.engine)
        existing_tables = set(inspector.get_table_names())
        missing_tables = sorted(set(metadata.tables) - existing_tables)
        missing_columns: dict[str, list[str]] = {}
        nullable_columns: dict[str, list[str]] = {}
        for table_name, table in metadata.tables.items():
            if table_name not in existing_tables:
                continue
            reflected_columns = {
                item["name"]: item for item in inspector.get_columns(table_name)
            }
            existing_columns = set(reflected_columns)
            missing = sorted(
                column.name for column in table.columns if column.name not in existing_columns
            )
            if missing:
                missing_columns[table_name] = missing
            invalid_nullable = sorted(
                column.name
                for column in table.columns
                if column.name in reflected_columns
                and not column.nullable
                and reflected_columns[column.name].get("nullable") is not False
            )
            if invalid_nullable:
                nullable_columns[table_name] = invalid_nullable
        if missing_tables or missing_columns or nullable_columns:
            details: list[str] = []
            if missing_tables:
                details.append(f"missing tables={','.join(missing_tables)}")
            if missing_columns:
                details.append(
                    "missing columns="
                    + ";".join(
                        f"{table}({','.join(columns)})"
                        for table, columns in sorted(missing_columns.items())
                    )
                )
            if nullable_columns:
                details.append(
                    "nullable columns="
                    + ";".join(
                        f"{table}({','.join(columns)})"
                        for table, columns in sorted(nullable_columns.items())
                    )
                )
            raise ValueError(
                "PostgreSQL schema is not fully migrated; apply numbered migrations: "
                + " ".join(details)
            )

    def _ensure_lease_claim_column(self) -> None:
        """Apply the additive account-scope claimant column to old stores."""

        columns = {item["name"] for item in inspect(self.engine).get_columns("worker_leases")}
        if "claimed_workload_id" in columns:
            return
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE worker_leases "
                        "ADD COLUMN claimed_workload_id VARCHAR(128)"
                    )
                )
                connection.execute(
                    text(
                        "UPDATE worker_leases SET claimed_workload_id = workload_id "
                        "WHERE claimed_workload_id IS NULL"
                    )
                )
        except OperationalError:
            # Another worker may have applied this additive change concurrently.
            columns = {
                item["name"] for item in inspect(self.engine).get_columns("worker_leases")
            }
            if "claimed_workload_id" not in columns:
                raise

    def _ensure_data_latest_column(self) -> None:
        """Apply the additive worker-status column for existing local stores."""

        columns = {
            item["name"] for item in inspect(self.engine).get_columns("worker_states")
        }
        if "data_latest_at" in columns:
            return
        column_type = "TIMESTAMPTZ" if self.engine.dialect.name == "postgresql" else "DATETIME"
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    text(f"ALTER TABLE worker_states ADD COLUMN data_latest_at {column_type}")
                )
        except OperationalError:
            # Another worker may have applied this additive change concurrently.
            columns = {
                item["name"] for item in inspect(self.engine).get_columns("worker_states")
            }
            if "data_latest_at" not in columns:
                raise

    def _ensure_lease_generation_column(self) -> None:
        """Add the local-store fence generation without rewriting lease rows."""

        columns = {item["name"] for item in inspect(self.engine).get_columns("worker_leases")}
        if "fence_generation" in columns:
            return
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE worker_leases "
                        "ADD COLUMN fence_generation BIGINT NOT NULL DEFAULT 1"
                    )
                )
        except OperationalError:
            columns = {
                item["name"] for item in inspect(self.engine).get_columns("worker_leases")
            }
            if "fence_generation" not in columns:
                raise

    def _ensure_paper_authorization_columns(self) -> None:
        """Add immutable order-bound columns to existing local databases."""

        columns = {item["name"] for item in inspect(self.engine).get_columns("paper_orders")}
        definitions = (
            (
                "authorization_max_new_notional",
                "NUMERIC(28,12) NOT NULL DEFAULT 0",
            ),
            (
                "authorization_max_reduction_notional",
                "NUMERIC(28,12) NOT NULL DEFAULT 0",
            ),
            ("authorization_costs_included", "BOOLEAN NOT NULL DEFAULT TRUE"),
        )
        for name, definition in definitions:
            if name in columns:
                continue
            try:
                with self.engine.begin() as connection:
                    connection.execute(
                        text(f"ALTER TABLE paper_orders ADD COLUMN {name} {definition}")
                    )
            except OperationalError:
                columns = {
                    item["name"] for item in inspect(self.engine).get_columns("paper_orders")
                }
                if name not in columns:
                    raise

    def health_check(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def status(self, workload_id: str) -> WorkerStatus:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(worker_states).where(worker_states.c.workload_id == workload_id)
            ).mappings().one_or_none()
        if row is None:
            return WorkerStatus.unknown(workload_id)
        return WorkerStatus(
            workload_id=workload_id,
            worker_state=WorkerState(row["worker_state"]),
            health=WorkerHealth(row["health"]),
            mode=row["mode"],
            heartbeat_at=_utc(row["heartbeat_at"]),
            last_cycle_at=_utc(row["last_cycle_at"]),
            data_status=row["data_status"],
            data_reason=row["data_reason"],
            data_latest_at=_utc(row["data_latest_at"]),
            eligible_strategy_count=row["eligible_strategy_count"],
            active_risk_locks=tuple(row["active_risk_locks"] or ()),
            reconciliation_status=row["reconciliation_status"],
            last_decision=row["last_decision"],
            last_reason_codes=tuple(row["last_reason_codes"] or ()),
            paper_order_count=row["paper_order_count"],
            paper_fill_count=row["paper_fill_count"],
            stop_requested=bool(row["stop_requested"]),
            last_error=row["last_error"],
            last_trace=tuple(
                DecisionTraceEvent(
                    stage=item["stage"],
                    status=item["status"],
                    reason=item.get("reason"),
                    metadata=(
                        tuple(
                            (str(key), str(value))
                            for key, value in item["metadata"].items()
                        )
                        if isinstance(item.get("metadata"), dict)
                        else ()
                    ),
                )
                for item in (row["last_trace"] or ())
            ),
        )

    def strategy_health(
        self,
        workload_id: str,
        *,
        cycle_limit: int = DEFAULT_STRATEGY_HEALTH_CYCLE_LIMIT,
    ) -> tuple[StrategyHealthMetric, ...]:
        """Aggregate signal counters from a bounded trace window."""

        return self.strategy_health_report(workload_id, cycle_limit=cycle_limit).metrics

    def strategy_health_report(
        self,
        workload_id: str,
        *,
        cycle_limit: int = DEFAULT_STRATEGY_HEALTH_CYCLE_LIMIT,
    ) -> StrategyHealthReport:
        """Return bounded signal counters from existing durable cycle traces.

        Legacy traces without structured strategy metadata are ignored rather
        than inferred.  This keeps the projection honest and avoids treating
        a free-form reason string as an authoritative measurement. Replayed
        cycles remain part of the bounded cycle window but do not count as new
        strategy evaluations.
        """

        if cycle_limit <= 0:
            raise ValueError("strategy health cycle_limit must be positive")
        with self.engine.connect() as connection:
            cycle_rows = connection.execute(
                select(worker_cycles.c.replayed, worker_cycles.c.decision_trace)
                .where(worker_cycles.c.workload_id == workload_id)
                .order_by(
                    worker_cycles.c.completed_at.desc(),
                    worker_cycles.c.started_at.desc(),
                    worker_cycles.c.cycle_id.desc(),
                )
                .limit(cycle_limit + 1)
            ).all()
        truncated = len(cycle_rows) > cycle_limit
        cycle_rows = list(reversed(cycle_rows[:cycle_limit]))
        replayed_cycle_count = sum(1 for replayed, _ in cycle_rows if replayed)
        structured_decision_count = 0
        unattributed_decision_count = 0
        metrics: tuple[StrategyHealthMetric, ...]
        counters: dict[tuple[str, str], list[int]] = {}
        for replayed, trace in cycle_rows:
            if replayed:
                continue
            if not isinstance(trace, list):
                continue
            for event in trace:
                if not isinstance(event, dict) or event.get("stage") != "strategy_decision":
                    continue
                metadata = event.get("metadata")
                if not isinstance(metadata, dict):
                    unattributed_decision_count += 1
                    continue
                strategy_id = metadata.get("strategy_id")
                strategy_version = metadata.get("strategy_version")
                direction = metadata.get("direction")
                try:
                    intent_count = int(metadata.get("intent_count", "-1"))
                except (TypeError, ValueError):
                    unattributed_decision_count += 1
                    continue
                if not isinstance(strategy_id, str) or not strategy_id.strip():
                    unattributed_decision_count += 1
                    continue
                if not isinstance(strategy_version, str) or not strategy_version.strip():
                    unattributed_decision_count += 1
                    continue
                if direction not in {"long", "short", "flat", "hold"}:
                    unattributed_decision_count += 1
                    continue
                if intent_count < 0:
                    unattributed_decision_count += 1
                    continue
                structured_decision_count += 1
                key = (strategy_id, strategy_version)
                bucket = counters.setdefault(key, [0, 0, 0, 0, 0, 0, 0])
                bucket[0] += 1
                bucket[{"long": 1, "short": 2, "flat": 3, "hold": 4}[direction]] += 1
                bucket[5] += intent_count
                if intent_count > 0:
                    bucket[6] += 1

        metrics = tuple(
            StrategyHealthMetric(
                strategy_id=key[0],
                strategy_version=key[1],
                decision_count=values[0],
                long_signal_count=values[1],
                short_signal_count=values[2],
                flat_signal_count=values[3],
                hold_signal_count=values[4],
                intent_count=values[5],
                intent_decision_count=values[6],
            )
            for key, values in sorted(counters.items())
        )
        return StrategyHealthReport(
            workload_id=workload_id,
            cycle_limit=cycle_limit,
            cycles_considered=len(cycle_rows),
            truncated=truncated,
            replayed_cycle_count=replayed_cycle_count,
            structured_decision_count=structured_decision_count,
            unattributed_decision_count=unattributed_decision_count,
            metrics=metrics,
        )

    def observed_status(
        self, workload_id: str, *, now: datetime, lease_key: str | None = None
    ) -> WorkerStatus:
        """Return status with expired active-worker leases surfaced fail-closed."""

        require_utc(now)
        current = self.status(workload_id)
        if current.worker_state not in {
            WorkerState.STARTING,
            WorkerState.RUNNING,
            WorkerState.STOPPING,
        }:
            return current
        claimant = self.lease_claimant(workload_id, now=now, lease_key=lease_key)
        if claimant != workload_id:
            return replace(
                current,
                worker_state=WorkerState.UNKNOWN,
                health=WorkerHealth.FAILED,
                last_error=(
                    "WORKER_LEASE_OWNED_BY_OTHER_WORKLOAD"
                    if claimant is not None
                    else "WORKER_LEASE_EXPIRED"
                ),
            )
        if self.lease_is_valid(
            workload_id, current_owner=None, now=now, lease_key=lease_key
        ):
            return current
        return replace(
            current,
            worker_state=WorkerState.UNKNOWN,
            health=WorkerHealth.FAILED,
            last_error="WORKER_LEASE_EXPIRED",
        )

    def lease_claimant(
        self, workload_id: str, *, now: datetime, lease_key: str | None = None
    ) -> str | None:
        """Return the active workload claiming a coordination lease."""

        require_utc(now)
        coordination_key = lease_key or workload_id
        with self.engine.connect() as connection:
            row = connection.execute(
                select(
                    worker_leases.c.claimed_workload_id,
                    worker_leases.c.expires_at,
                ).where(worker_leases.c.workload_id == coordination_key)
            ).mappings().one_or_none()
        if row is None:
            return None
        expires_at = _utc(row["expires_at"])
        if expires_at is None or expires_at <= now:
            return None
        return cast(str | None, row["claimed_workload_id"])

    def lease_is_valid(
        self,
        workload_id: str,
        current_owner: str | None,
        *,
        now: datetime,
        lease_key: str | None = None,
    ) -> bool:
        """Return whether a workload lease is unexpired and optionally owned."""

        require_utc(now)
        coordination_key = lease_key or workload_id
        with self.engine.connect() as connection:
            row = connection.execute(
                select(
                    worker_leases.c.owner_id,
                    worker_leases.c.claimed_workload_id,
                    worker_leases.c.expires_at,
                ).where(worker_leases.c.workload_id == coordination_key)
            ).mappings().one_or_none()
        if (
            row is None
            or row["claimed_workload_id"] != workload_id
            or (current_owner is not None and row["owner_id"] != current_owner)
        ):
            return False
        expires_at = _utc(row["expires_at"])
        return expires_at is not None and expires_at > now

    @staticmethod
    def _status_values(status: WorkerStatus, *, updated_at: datetime) -> dict[str, object]:
        return {
            "workload_id": status.workload_id,
            "worker_state": status.worker_state.value,
            "health": status.health.value,
            "mode": status.mode,
            "heartbeat_at": status.heartbeat_at,
            "last_cycle_at": status.last_cycle_at,
            "stop_requested": status.stop_requested,
            "data_status": status.data_status,
            "data_reason": status.data_reason,
            "data_latest_at": status.data_latest_at,
            "eligible_strategy_count": status.eligible_strategy_count,
            "active_risk_locks": list(status.active_risk_locks),
            "reconciliation_status": status.reconciliation_status,
            "last_decision": status.last_decision,
            "last_reason_codes": list(status.last_reason_codes),
            "paper_order_count": status.paper_order_count,
            "paper_fill_count": status.paper_fill_count,
            "last_error": status.last_error,
            "last_trace": [event.as_dict() for event in status.last_trace],
            "updated_at": updated_at,
        }

    def save_status(self, status: WorkerStatus, *, updated_at: datetime) -> None:
        require_utc(updated_at)
        row = self._status_values(status, updated_at=updated_at)
        with self._transaction(immediate_sqlite=True) as connection:
            existing = connection.execute(
                select(worker_states.c.workload_id).where(
                    worker_states.c.workload_id == status.workload_id
                )
            ).first()
            if existing is None:
                connection.execute(worker_states.insert().values(**row))
            else:
                connection.execute(
                    update(worker_states)
                    .where(worker_states.c.workload_id == status.workload_id)
                    .values(**row)
                )

    def save_status_if_lease_owned(
        self,
        status: WorkerStatus,
        *,
        updated_at: datetime,
        owner_id: str,
        lease_key: str,
        preserve_stop_request: bool = True,
    ) -> bool:
        """Write status only while this owner holds an unexpired lease."""

        require_utc(updated_at)
        row = self._status_values(status, updated_at=updated_at)
        with self._transaction(immediate_sqlite=True) as connection:
            lease = connection.execute(
                select(
                    worker_leases.c.owner_id,
                    worker_leases.c.claimed_workload_id,
                    worker_leases.c.expires_at,
                    worker_leases.c.fence_generation,
                )
                .where(worker_leases.c.workload_id == lease_key)
                .with_for_update()
            ).mappings().one_or_none()
            expires_at = _utc(lease["expires_at"]) if lease is not None else None
            if (
                lease is None
                or lease["owner_id"] != owner_id
                or lease["claimed_workload_id"] != status.workload_id
                or expires_at is None
                or expires_at <= updated_at
            ):
                return False
            existing_stop_request = connection.execute(
                select(worker_states.c.stop_requested)
                .where(worker_states.c.workload_id == status.workload_id)
                .with_for_update()
            ).scalar_one_or_none()
            if existing_stop_request is None:
                connection.execute(worker_states.insert().values(**row))
            else:
                if (
                    preserve_stop_request
                    and bool(existing_stop_request)
                    and not status.stop_requested
                ):
                    row["stop_requested"] = True
                    if status.worker_state is WorkerState.RUNNING:
                        row["worker_state"] = WorkerState.STOPPING.value
                connection.execute(
                    update(worker_states)
                    .where(worker_states.c.workload_id == status.workload_id)
                    .values(**row)
                )
            return True

    def set_stop_requested(self, workload_id: str, requested: bool, *, now: datetime) -> None:
        """Set the operator stop flag without clobbering newer worker state.

        The stop command is intentionally allowed to write without owning the
        worker lease, but it must not perform an unlocked read-modify-write of
        the whole status projection. Locking the row and updating only the
        control fields preserves a concurrent heartbeat, decision trace, and
        cycle result.
        """

        require_utc(now)
        with self._transaction(immediate_sqlite=True) as connection:
            row = connection.execute(
                select(worker_states)
                .where(worker_states.c.workload_id == workload_id)
                .with_for_update()
            ).mappings().one_or_none()
            if row is None:
                unknown = replace(WorkerStatus.unknown(workload_id), stop_requested=requested)
                connection.execute(
                    worker_states.insert().values(
                        **self._status_values(unknown, updated_at=now)
                    )
                )
                return

            next_state = row["worker_state"]
            if requested and next_state == WorkerState.RUNNING.value:
                next_state = WorkerState.STOPPING.value
            connection.execute(
                update(worker_states)
                .where(worker_states.c.workload_id == workload_id)
                .values(
                    worker_state=next_state,
                    stop_requested=requested,
                    updated_at=now,
                )
            )

    def decision_seen(self, workload_id: str, decision_key: str) -> bool:
        return self.decision_health(workload_id, decision_key) is not None

    def decision_health(self, workload_id: str, decision_key: str) -> WorkerHealth | None:
        """Return the latest completed health for a durable decision, if present."""

        with self.engine.connect() as connection:
            value = connection.execute(
                select(worker_cycles.c.cycle_status)
                .where(
                    worker_cycles.c.workload_id == workload_id,
                    worker_cycles.c.decision_key == decision_key,
                )
                .order_by(worker_cycles.c.completed_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        return None if value is None else WorkerHealth(value)

    def acquire_lease(
        self,
        workload_id: str,
        owner_id: str,
        *,
        now: datetime,
        duration: timedelta,
        lease_key: str | None = None,
    ) -> ExecutionAuthority | None:
        require_utc(now)
        coordination_key = lease_key or workload_id
        expires_at = now + duration
        try:
            with self._transaction(immediate_sqlite=True) as connection:
                row = connection.execute(
                    select(worker_leases)
                    .where(worker_leases.c.workload_id == coordination_key)
                    .with_for_update()
                ).mappings().one_or_none()
                current_expires_at = _utc(row["expires_at"]) if row is not None else None
                current_owner = row["owner_id"] if row is not None else None
                current_claim = row["claimed_workload_id"] if row is not None else None
                active_claim = current_expires_at is not None and current_expires_at > now
                if (
                    active_claim
                    and (current_owner != owner_id or current_claim != workload_id)
                ):
                    return None
                acquired_at = row["acquired_at"] if row is not None and active_claim else now
                generation = (
                    int(row["fence_generation"])
                    if row is not None and active_claim
                    else (int(row["fence_generation"]) + 1 if row is not None else 1)
                )
                values = {
                    "claimed_workload_id": workload_id,
                    "owner_id": owner_id,
                    "acquired_at": acquired_at,
                    "heartbeat_at": now,
                    "expires_at": expires_at,
                    "fence_generation": generation,
                }
                if row is None:
                    connection.execute(
                        worker_leases.insert().values(workload_id=coordination_key, **values)
                    )
                else:
                    connection.execute(
                        update(worker_leases)
                        .where(worker_leases.c.workload_id == coordination_key)
                        .values(**values)
                    )
                return ExecutionAuthority(
                    workload_id=workload_id,
                    lease_key=coordination_key,
                    owner_id=owner_id,
                    generation=generation,
                    observed_at=now,
                    expires_at=expires_at,
                )
        except IntegrityError:
            # A concurrent first acquisition won the primary-key race.
            return None

    def heartbeat(
        self,
        workload_id: str,
        owner_id: str,
        *,
        now: datetime,
        duration: timedelta,
        lease_key: str | None = None,
    ) -> ExecutionAuthority | None:
        require_utc(now)
        coordination_key = lease_key or workload_id
        with self._transaction(immediate_sqlite=True) as connection:
            row = connection.execute(
                select(
                    worker_leases.c.owner_id,
                    worker_leases.c.claimed_workload_id,
                    worker_leases.c.expires_at,
                    worker_leases.c.fence_generation,
                )
                .where(worker_leases.c.workload_id == coordination_key)
                .with_for_update()
            ).mappings().one_or_none()
            expires_at = _utc(row["expires_at"]) if row is not None else None
            if (
                row is None
                or row["owner_id"] != owner_id
                or row["claimed_workload_id"] != workload_id
                or expires_at is None
                or expires_at <= now
            ):
                return None
            generation = int(row["fence_generation"])
            next_expiry = now + duration
            result = connection.execute(
                update(worker_leases)
                .where(
                    worker_leases.c.workload_id == coordination_key,
                    worker_leases.c.owner_id == owner_id,
                    worker_leases.c.claimed_workload_id == workload_id,
                )
                .values(heartbeat_at=now, expires_at=next_expiry)
            )
            if result.rowcount != 1:
                return None
            return ExecutionAuthority(
                workload_id=workload_id,
                lease_key=coordination_key,
                owner_id=owner_id,
                generation=generation,
                observed_at=now,
                expires_at=next_expiry,
            )

    def release_lease(
        self, workload_id: str, owner_id: str, *, lease_key: str | None = None
    ) -> None:
        coordination_key = lease_key or workload_id
        with self.engine.begin() as connection:
            connection.execute(
                delete(worker_leases).where(
                    worker_leases.c.workload_id == coordination_key,
                    worker_leases.c.owner_id == owner_id,
                    worker_leases.c.claimed_workload_id == workload_id,
                )
            )

    def record_cycle(
        self,
        *,
        workload_id: str,
        decision_key: str,
        started_at: datetime,
        completed_at: datetime,
        outcome: CycleOutcome,
    ) -> CycleOutcome:
        persisted = self._record_cycle(
            workload_id=workload_id,
            decision_key=decision_key,
            started_at=started_at,
            completed_at=completed_at,
            outcome=outcome,
        )
        assert persisted is not None
        return persisted

    def record_cycle_if_lease_owned(
        self,
        *,
        workload_id: str,
        decision_key: str,
        started_at: datetime,
        completed_at: datetime,
        outcome: CycleOutcome,
        owner_id: str,
        lease_key: str,
    ) -> CycleOutcome | None:
        """Append a cycle only while the owner holds the coordination lease.

        The lease row and cycle insert share one transaction.  This prevents
        a stale worker from appending a blocked or failed result after a
        replacement worker has reclaimed the workload.
        """

        return self._record_cycle(
            workload_id=workload_id,
            decision_key=decision_key,
            started_at=started_at,
            completed_at=completed_at,
            outcome=outcome,
            owner_id=owner_id,
            lease_key=lease_key,
        )

    def _record_cycle(
        self,
        *,
        workload_id: str,
        decision_key: str,
        started_at: datetime,
        completed_at: datetime,
        outcome: CycleOutcome,
        owner_id: str | None = None,
        lease_key: str | None = None,
    ) -> CycleOutcome | None:
        require_utc(started_at)
        require_utc(completed_at)
        if completed_at < started_at:
            raise ValueError("worker cycle completion cannot precede its start")
        with self._transaction(immediate_sqlite=True) as connection:
            if owner_id is not None:
                coordination_key = lease_key or workload_id
                lease = connection.execute(
                    select(
                        worker_leases.c.owner_id,
                        worker_leases.c.claimed_workload_id,
                        worker_leases.c.expires_at,
                    )
                    .where(worker_leases.c.workload_id == coordination_key)
                    .with_for_update()
                ).mappings().one_or_none()
                expires_at = _utc(lease["expires_at"]) if lease is not None else None
                if (
                    lease is None
                    or lease["owner_id"] != owner_id
                    or lease["claimed_workload_id"] != workload_id
                    or expires_at is None
                    or expires_at <= completed_at
                ):
                    return None
            prior = connection.execute(
                select(worker_cycles.c.cycle_id, worker_cycles.c.cycle_status).where(
                    worker_cycles.c.workload_id == workload_id,
                    worker_cycles.c.decision_key == decision_key,
                )
                .order_by(worker_cycles.c.completed_at.desc())
                .limit(1)
            ).mappings().one_or_none()
            replayed = prior is not None and prior["cycle_status"] != WorkerHealth.FAILED.value
            row = {
                "cycle_id": outcome.cycle_id,
                "workload_id": workload_id,
                "decision_key": decision_key,
                "started_at": started_at,
                "completed_at": completed_at,
                "cycle_status": outcome.health.value,
                "action": outcome.action,
                "reason_codes": list(
                    tuple(dict.fromkeys((*outcome.reason_codes, "DUPLICATE_DECISION")))
                    if replayed
                    else outcome.reason_codes
                ),
                "decision_trace": [
                    event.as_dict()
                    for event in (
                        (
                            *outcome.trace,
                            DecisionTraceEvent("idempotency", "REPLAYED", "DUPLICATE_DECISION"),
                        )
                        if replayed
                        and not any(event.stage == "idempotency" for event in outcome.trace)
                        else outcome.trace
                    )
                ],
                "replayed": replayed,
            }
            try:
                connection.execute(worker_cycles.insert().values(**row))
            except IntegrityError:
                raise
            return CycleOutcome(
                cycle_id=outcome.cycle_id,
                decision_key=outcome.decision_key,
                action=outcome.action,
                health=outcome.health,
                data_status=outcome.data_status,
                data_reason=outcome.data_reason,
                eligible_strategy_count=outcome.eligible_strategy_count,
                reconciliation_status=outcome.reconciliation_status,
                reason_codes=(
                    tuple(dict.fromkeys((*outcome.reason_codes, "DUPLICATE_DECISION")))
                    if replayed
                    else outcome.reason_codes
                ),
                trace=(
                    (
                        *outcome.trace,
                        DecisionTraceEvent("idempotency", "REPLAYED", "DUPLICATE_DECISION"),
                    )
                    if replayed
                    and not any(event.stage == "idempotency" for event in outcome.trace)
                    else outcome.trace
                ),
                replayed=replayed,
            )

    def cycle_count(self, workload_id: str) -> int:
        with self.engine.connect() as connection:
            return len(
                connection.execute(
                    select(worker_cycles.c.cycle_id).where(
                        worker_cycles.c.workload_id == workload_id
                    )
                ).all()
            )


__all__ = ["WorkerStore"]
