"""Disposable PostgreSQL migration, rollback, recovery, and revision-race tests."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from multiprocessing import Barrier, Queue, get_context
from pathlib import Path
from queue import Empty

import pytest
from sqlalchemy import text

from traderos.database.connection import create_database_engine
from traderos.research.provenance import ProvenanceQueryService
from traderos.research.registry import ExperimentRegistry
from traderos.research.strategy_lifecycle import (
    ActorCapability,
    ActorDirectory,
    FeatureDefinition,
    LifecycleError,
    RegisteredImplementation,
    StrategyArtifact,
    StrategyParameter,
    StrategyStage,
)
from traderos.research.strategy_service import StrategyGovernanceService
from traderos.research.strategy_store import StrategyGovernanceStore

POSTGRES_URL_ENV = "TRADEROS_POSTGRES_TEST_URL"
BASE = datetime(2024, 1, 2, tzinfo=UTC)
type RaceResult = tuple[str, str]


def _artifact() -> StrategyArtifact:
    return StrategyArtifact(
        strategy_id="postgres_strategy",
        strategy_version="1",
        family_id="postgres-family",
        author="researcher",
        implementation=RegisteredImplementation("always_flat", "1"),
        instruments=("EUR/USD",),
        timeframes=("1h",),
        parameters=(StrategyParameter("lookback", 3),),
        features=(FeatureDefinition("return", "1"),),
        training_reference=None,
        validation_reference=None,
        oos_reference=None,
        forecast_semantics="software test signal",
        holding_period=None,
        turnover_assumption=None,
        cost_model_reference=None,
        capacity_model_reference=None,
        regime_dependencies=(),
        risk_dependencies=("risk-v1",),
        known_failure_modes=("fixture only",),
        code_identity="git:postgres-test",
        dataset_identity="dataset-test",
        configuration_identity=None,
        environment_reference="pytest",
    )


def _service(url: str) -> StrategyGovernanceService:
    store = StrategyGovernanceStore(create_database_engine(url))
    actors = ActorDirectory({"researcher": (ActorCapability.RESEARCH,)})
    return StrategyGovernanceService(store, actors)


def _worker(url: str, barrier: Barrier, results: Queue[RaceResult], key: str) -> None:
    service = _service(url)
    try:
        barrier.wait(timeout=20)
        service.transition_strategy(
            "postgres_strategy",
            "1",
            target_stage=StrategyStage.RESEARCH,
            actor_id="researcher",
            expected_revision=0,
            idempotency_key=key,
            reason="race test",
        )
        results.put(("ok", key))
    except Exception as exc:  # parent asserts the fail-closed race result
        results.put(("error", type(exc).__name__))
    finally:
        service.store.engine.dispose()


@pytest.fixture
def postgres_url() -> str:
    url = os.environ.get(POSTGRES_URL_ENV)
    if not url:
        pytest.skip(f"{POSTGRES_URL_ENV} is required for disposable PostgreSQL verification")
    return url


@pytest.fixture
def postgres_store(postgres_url: str) -> StrategyGovernanceStore:
    store = StrategyGovernanceStore(create_database_engine(postgres_url))
    with store.engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE strategy_lifecycle_events, strategy_lifecycle_state, "
                "strategy_approval_revocations, strategy_evidence_revocations, "
                "strategy_approvals, strategy_evidence, strategy_artifacts CASCADE"
            )
        )
    yield store
    store.engine.dispose()


@pytest.mark.integration
def test_postgres_governance_schema_and_transaction_rollback(
    postgres_store: StrategyGovernanceStore,
) -> None:
    service = StrategyGovernanceService(
        postgres_store,
        ActorDirectory({"researcher": (ActorCapability.RESEARCH,)}),
    )
    artifact = _artifact()
    service.register_strategy_artifact(artifact, actor_id="researcher")
    with postgres_store.engine.connect() as connection:
        tables = set(
            connection.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                    "AND tablename LIKE 'strategy_%'"
                )
            )
            .scalars()
            .all()
        )
    assert tables == {
        "strategy_artifacts",
        "strategy_evidence",
        "strategy_approvals",
        "strategy_evidence_revocations",
        "strategy_approval_revocations",
        "strategy_lifecycle_state",
        "strategy_lifecycle_events",
    }

    with postgres_store.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE OR REPLACE FUNCTION strategy_test_fail() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'test rollback'; END; $$"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER strategy_test_fail_trigger "
                "BEFORE INSERT ON strategy_lifecycle_events FOR EACH ROW "
                "EXECUTE FUNCTION strategy_test_fail()"
            )
        )
    with pytest.raises(LifecycleError, match="transaction failed"):
        service.transition_strategy(
            "postgres_strategy",
            "1",
            target_stage=StrategyStage.RESEARCH,
            actor_id="researcher",
            expected_revision=0,
            idempotency_key="rollback",
            reason="must rollback",
        )
    with postgres_store.engine.begin() as connection:
        connection.execute(
            text("DROP TRIGGER strategy_test_fail_trigger ON strategy_lifecycle_events")
        )
        connection.execute(text("DROP FUNCTION strategy_test_fail()"))
    assert postgres_store.get_state("postgres_strategy", "1").revision == 0
    assert postgres_store.list_history("postgres_strategy", "1") == ()


@pytest.mark.integration
def test_postgres_concurrent_transitions_allow_one_revision_and_recover(
    postgres_store: StrategyGovernanceStore, postgres_url: str
) -> None:
    service = StrategyGovernanceService(
        postgres_store,
        ActorDirectory({"researcher": (ActorCapability.RESEARCH,)}),
    )
    service.register_strategy_artifact(_artifact(), actor_id="researcher")
    context = get_context("fork")
    barrier = context.Barrier(2)
    results: Queue[RaceResult] = context.Queue()
    processes = [
        context.Process(target=_worker, args=(postgres_url, barrier, results, f"race-{index}"))
        for index in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
    values: list[RaceResult] = []
    for _ in processes:
        try:
            values.append(results.get(timeout=5))
        except Empty as exc:
            raise AssertionError("concurrent governance worker did not report") from exc
    assert sorted(item[0] for item in values) == ["error", "ok"]
    assert postgres_store.get_state("postgres_strategy", "1").revision == 1
    assert len(postgres_store.list_history("postgres_strategy", "1")) == 1
    restarted = _service(postgres_url)
    assert restarted.reconcile_strategy("postgres_strategy", "1").revision == 1
    restarted.store.engine.dispose()


@pytest.mark.integration
def test_postgres_provenance_reads_one_coherent_governance_revision(
    postgres_store: StrategyGovernanceStore,
    postgres_url: str,
    tmp_path: Path,
) -> None:
    service = StrategyGovernanceService(
        postgres_store,
        ActorDirectory({"researcher": (ActorCapability.RESEARCH,)}),
    )
    service.register_strategy_artifact(_artifact(), actor_id="researcher")
    reader = ProvenanceQueryService(
        postgres_store,
        ExperimentRegistry(tmp_path / "experiments"),
        ActorDirectory({"reader": (ActorCapability.PROVENANCE_READ,)}),
    )
    context = get_context("fork")
    barrier = context.Barrier(2)
    results: Queue[RaceResult] = context.Queue()
    process = context.Process(
        target=_worker, args=(postgres_url, barrier, results, "provenance-race")
    )
    process.start()
    barrier.wait(timeout=20)
    graph = reader.get_strategy_provenance("postgres_strategy", "1", actor_id="reader")
    process.join(timeout=30)
    assert graph.governance_revision in (0, 1)
    state_nodes = [node for node in graph.nodes if node.entity_type.value == "strategy_state"]
    assert len(state_nodes) == 1
    state_revision = dict(state_nodes[0].attributes)["revision"]
    assert state_revision == graph.governance_revision
    event_revisions = [
        int(dict(node.attributes)["resulting_revision"])
        for node in graph.nodes
        if node.entity_type.value == "lifecycle_event"
        and "resulting_revision" in dict(node.attributes)
    ]
    assert all(revision <= (graph.governance_revision or 0) for revision in event_revisions)
    assert results.get(timeout=5)[0] == "ok"
