"""Safety contracts for the unattended PAPER supervisor."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

import traderos.worker.worker as worker_module
from traderos.core.config import Settings, TradingMode
from traderos.data.bars import MarketBar
from traderos.data.errors import DataValidationError, TimestampNormalizationError
from traderos.data.hashing import market_bar_content_hash
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.lineage import AdjustmentPolicy, DatasetMetadata
from traderos.data.quality import DataQualityEvent, QualityCode, QualitySeverity
from traderos.data.storage import InMemoryMarketDataStore, MarketDataStore
from traderos.data.timeframes import Timeframe
from traderos.database.connection import create_database_engine
from traderos.database.schema import metadata, worker_leases
from traderos.monitoring import HealthCheck
from traderos.paper.engine import PaperTradingEngine
from traderos.paper.models import PaperQuote
from traderos.research.dataset import DatasetQualificationStatus, ResearchDatasetEligibility
from traderos.research.dataset_records import (
    DatasetQualificationRecord,
    DatasetQualificationRegistry,
)
from traderos.risk import (
    RiskAction,
    RiskAuthorization,
    RiskDecision,
    RiskDecisionStatus,
    RiskFirewall,
    SystemHealthSnapshot,
    SystemHealthStatus,
    risk_decision_integrity_id,
)
from traderos.signals import FusionDirection
from traderos.worker import (
    DataAdmission,
    PaperWorker,
    WorkerBusy,
    WorkerConfigurationError,
    WorkerDependencies,
    WorkerError,
    WorkerHealth,
    WorkerState,
    WorkerStatus,
)
from traderos.worker.models import (
    CycleOutcome,
    DecisionTraceEvent,
    StrategyHealthMetric,
    StrategyHealthReport,
)
from traderos.worker.pipeline import (
    PaperDecisionPipeline,
    PaperPipelineOutcome,
    PipelineTraceEvent,
)
from traderos.worker.store import WorkerStore, _utc
from traderos.worker.worker import (
    _is_ready,
    _parse_strategy_versions,
    _print_status,
    _print_strategy_health,
    _worker_run_exit_code,
    main,
)


def _worker(tmp_path, **overrides: object) -> PaperWorker:
    settings = Settings(
        _env_file=None,
        trading_mode=TradingMode.PAPER,
        database_url=f"sqlite:///{tmp_path / 'worker.db'}",
        **overrides,
    )
    return PaperWorker.from_settings(settings)


def _write_test_qualification(
    tmp_path,
    content_hash: str,
    checked_at: datetime,
    *,
    verdict: DatasetQualificationStatus = DatasetQualificationStatus.QUALIFIED,
    empirical_eligibility: ResearchDatasetEligibility = (
        ResearchDatasetEligibility.EMPIRICALLY_QUALIFIED_DATASET
    ),
    fixture: bool = False,
) -> DatasetQualificationRecord:
    """Create a temporary qualification double; this is not empirical evidence."""

    record = DatasetQualificationRecord(
        dataset_id="software-test-dataset",
        dataset_content_hash=content_hash,
        manifest_hash="software-test-manifest",
        policy_id="software-test-policy",
        policy_version="1",
        implementation_id="software-test-qualification",
        implementation_version="1",
        verdict=verdict,
        empirical_eligibility=empirical_eligibility,
        checked_at=checked_at,
        check_results=(("software_test", "passed"),),
        warnings=(),
        unavailable_checks=(),
        report_reference=None,
        fixture=fixture,
    )
    DatasetQualificationRegistry(tmp_path / "qualifications").record(record)
    return record


def _configured_dataset_worker(
    tmp_path,
    now: datetime,
    *,
    qualification: bool = False,
    qualification_hash: str | None = None,
    qualification_verdict: DatasetQualificationStatus = DatasetQualificationStatus.QUALIFIED,
) -> tuple[PaperWorker, str]:
    instrument = Instrument(
        canonical_symbol="EUR/USD",
        asset_class=AssetClass.FOREX,
        base_currency="EUR",
        quote_currency="USD",
    )
    bar = MarketBar(
        instrument=instrument,
        timeframe=Timeframe.H1,
        timestamp=now - timedelta(hours=1),
        open=Decimal("1.10"),
        high=Decimal("1.11"),
        low=Decimal("1.09"),
        close=Decimal("1.105"),
        source="local",
        ingestion_timestamp=now,
        bid=Decimal("1.104"),
        ask=Decimal("1.106"),
        quote_timestamp=now,
    )
    worker = _worker(
        tmp_path,
        worker_instrument=instrument.canonical_symbol,
        worker_timeframe=Timeframe.H1.value,
        worker_data_source="local",
        worker_dataset_version="qualified-test-data",
        worker_dataset_id="software-test-dataset",
    )
    worker.dependencies.market_store.upsert_bars((bar,))
    content_hash = market_bar_content_hash(
        worker.dependencies.market_store.query_bars(
            instrument.canonical_symbol,
            Timeframe.H1,
            bar.timestamp,
            now,
            source="local",
            adjustment_policy=AdjustmentPolicy.RAW,
        )
    )
    worker.dependencies.market_store.record_dataset(
        DatasetMetadata(
            dataset_version="qualified-test-data",
            dataset_hash=content_hash,
            provider="local",
            symbol=instrument.canonical_symbol,
            timeframe=Timeframe.H1.value,
            start=bar.timestamp,
            end=now,
            adjustment_policy=AdjustmentPolicy.RAW,
            configuration_version="software-test",
            quality_status="clean",
            created_at=now,
        )
    )
    if qualification:
        record = _write_test_qualification(
            tmp_path,
            qualification_hash or content_hash,
            now,
            verdict=qualification_verdict,
            empirical_eligibility=(
                ResearchDatasetEligibility.EMPIRICALLY_QUALIFIED_DATASET
                if qualification_verdict is DatasetQualificationStatus.QUALIFIED
                else ResearchDatasetEligibility.BLOCKED
            ),
        )
        worker.settings.worker_dataset_qualification_id = record.qualification_id
        worker.settings.worker_dataset_qualification_root = str(tmp_path / "qualifications")
        worker.dependencies = replace(
            worker.dependencies,
            dataset_qualification_registry=DatasetQualificationRegistry(
                tmp_path / "qualifications"
            ),
        )
    worker._clock = lambda: now
    return worker, content_hash


def test_missing_prerequisites_create_no_orders_and_record_no_trade(tmp_path) -> None:
    worker = _worker(tmp_path)

    status = worker.run_once()

    assert status.worker_state.value == "STOPPED"
    assert status.health.value == "BLOCKED"
    assert status.mode == "PAPER"
    assert status.data_status == "BLOCKED"
    assert status.eligible_strategy_count == 0
    assert status.last_decision == "NO_TRADE"
    assert "DATA_NOT_CONFIGURED" in status.last_reason_codes
    assert [event.stage for event in status.last_trace] == [
        "persistence",
        "reconciliation",
        "data_admission",
        "strategy_eligibility",
        "execution",
        "decision",
    ]
    assert status.last_trace[-1].reason == "ACCOUNT_NOT_CONFIGURED"
    assert worker.store.cycle_count("default") == 1
    assert worker.dependencies.paper_engine.store.orders("missing") == ()


def test_worker_rejects_duplicate_strategy_configuration() -> None:
    with pytest.raises(
        WorkerConfigurationError,
        match="must not contain duplicate strategy identities",
    ):
        _parse_strategy_versions("fixture_strategy@1,fixture_strategy:1")


def test_global_kill_switch_blocks_all_activity_and_records_no_trade(tmp_path) -> None:
    worker = _worker(
        tmp_path,
        kill_switch_active=True,
        worker_account_id="paper-account",
        worker_instrument="EUR/USD",
        worker_timeframe="1h",
        worker_strategy_versions="fixture_strategy@1",
    )

    status = worker.run_once()

    assert status.worker_state.value == "STOPPED"
    assert status.health.value == "BLOCKED"
    assert status.last_decision == "NO_TRADE"
    assert "KILL_SWITCH_ACTIVE" in status.last_reason_codes
    assert status.last_trace[0].stage == "global_gate"
    assert status.last_trace[0].status == "BLOCKED"
    assert [event.stage for event in status.last_trace] == [
        "global_gate",
        "persistence",
        "reconciliation",
        "data_admission",
        "strategy_eligibility",
        "execution",
        "decision",
    ]
    assert worker.dependencies.paper_engine.store.orders("paper-account") == ()


def test_status_marks_missing_execution_pipeline_unsupported(
    tmp_path, monkeypatch, capsys
) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    worker = _worker(
        tmp_path,
        worker_account_id="paper-account",
        worker_instrument="EUR/USD",
        worker_timeframe="1h",
        worker_strategy_versions="fixture_strategy@1",
    )
    monkeypatch.setattr(worker, "_reconcile", lambda: ("HEALTHY", (), 0, 0))
    monkeypatch.setattr(
        worker,
        "_admit_data",
        lambda _: DataAdmission("HEALTHY", None, now - timedelta(hours=1), "fixture", "hash"),
    )
    monkeypatch.setattr(worker, "_resolve_strategy_eligibility", lambda: (1, ()))
    worker.dependencies = WorkerDependencies(
        market_store=worker.dependencies.market_store,
        paper_engine=worker.dependencies.paper_engine,
        governance=worker.dependencies.governance,
        pipeline=None,
        system_health_provider=worker.dependencies.system_health_provider,
    )
    worker._clock = lambda: now

    status = worker.run_once()
    _print_status(status)
    output = capsys.readouterr().out

    assert status.health.value == "BLOCKED"
    assert "EXECUTION_PATH_UNSUPPORTED" in status.last_reason_codes
    assert "EXECUTION=UNSUPPORTED" in output


def test_worker_enters_pipeline_without_eligible_strategy_for_pending_management(
    tmp_path, monkeypatch
) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    worker = _worker(
        tmp_path,
        worker_account_id="paper-account",
        worker_instrument="EUR/USD",
        worker_timeframe="1h",
        worker_data_source="local",
        worker_dataset_version="fixture",
        worker_strategy_versions="fixture_strategy@1",
    )
    latest = MarketBar(
        instrument=Instrument(
            canonical_symbol="EUR/USD",
            asset_class=AssetClass.FOREX,
            base_currency="EUR",
            quote_currency="USD",
        ),
        timeframe=Timeframe.H1,
        timestamp=now - timedelta(hours=1),
        open=Decimal("1.10"),
        high=Decimal("1.11"),
        low=Decimal("1.09"),
        close=Decimal("1.105"),
        source="local",
        ingestion_timestamp=now,
        bid=Decimal("1.104"),
        ask=Decimal("1.106"),
        quote_timestamp=now,
    )
    monkeypatch.setattr(worker, "_reconcile", lambda: ("HEALTHY", (), 0, 0))
    monkeypatch.setattr(
        worker,
        "_admit_data",
        lambda _: DataAdmission("HEALTHY", None, latest.timestamp, "fixture", "hash"),
    )
    monkeypatch.setattr(
        worker,
        "_resolve_strategy_eligibility",
        lambda: (0, ("fixture_strategy@1:STRATEGY_DISABLED",)),
    )
    monkeypatch.setattr(
        worker.dependencies.market_store, "latest_bar", lambda *args, **kwargs: latest
    )
    monkeypatch.setattr(
        worker.dependencies.market_store, "query_bars", lambda *args, **kwargs: (latest,)
    )
    monkeypatch.setattr(worker.dependencies.market_store, "quality_events", lambda: ())
    pipeline_calls: list[dict[str, object]] = []

    class PendingManagementPipeline:
        def run(self, **kwargs: object) -> PaperPipelineOutcome:
            pipeline_calls.append(kwargs)
            return PaperPipelineOutcome(
                action="NO_TRADE",
                health="BLOCKED",
                reason_codes=("PENDING_MANAGEMENT_ONLY",),
                trace=(),
            )

    worker.dependencies = WorkerDependencies(
        market_store=worker.dependencies.market_store,
        paper_engine=worker.dependencies.paper_engine,
        governance=worker.dependencies.governance,
        pipeline=cast("PaperDecisionPipeline", PendingManagementPipeline()),
        system_health_provider=worker.dependencies.system_health_provider,
    )
    worker._clock = lambda: now

    status = worker.run_once()

    assert len(pipeline_calls) == 1
    assert pipeline_calls[0]["strategy_keys"] == (("fixture_strategy", "1"),)
    assert status.health is WorkerHealth.BLOCKED
    assert "PENDING_MANAGEMENT_ONLY" in status.last_reason_codes


def test_status_persists_and_renders_data_latest_timestamp(tmp_path, monkeypatch, capsys) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    latest = now - timedelta(hours=1)
    worker = _worker(tmp_path)
    monkeypatch.setattr(
        worker,
        "_admit_data",
        lambda _: DataAdmission("BLOCKED", "DATA_STALE", latest),
    )
    worker._clock = lambda: now

    status = worker.run_once()
    _print_status(status)
    output = capsys.readouterr().out

    assert status.data_latest_at == latest
    assert f"DATA_LATEST={latest.isoformat()}" in output
    assert "DATA_FRESHNESS=AGE_SECONDS:" in output


def test_status_json_is_machine_readable_and_contains_trace(tmp_path, capsys) -> None:
    status = _worker(tmp_path).run_once()

    _print_status(status, json_output=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload["worker_state"] == "STOPPED"
    assert payload["health"] == "BLOCKED"
    assert payload["last_decision"] == "NO_TRADE"
    assert payload["last_trace"][-1]["stage"] == "decision"
    assert payload["heartbeat_at"] is not None


def test_worker_close_disposes_database_engine_once(tmp_path, monkeypatch) -> None:
    worker = _worker(tmp_path)
    dispose_calls = 0

    def dispose() -> None:
        nonlocal dispose_calls
        dispose_calls += 1

    monkeypatch.setattr(worker.store.engine, "dispose", dispose)

    worker.close()
    worker.close()

    assert dispose_calls == 1


def test_from_settings_disposes_engine_when_dependency_setup_fails(tmp_path, monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        trading_mode=TradingMode.PAPER,
        database_url=f"sqlite:///{tmp_path / 'worker.db'}",
    )
    engine = create_database_engine(settings.database_url)
    dispose_calls = 0

    def dispose() -> None:
        nonlocal dispose_calls
        dispose_calls += 1

    def fail_bindings() -> tuple[object, ...]:
        raise RuntimeError("synthetic dependency setup failure")

    monkeypatch.setattr(worker_module, "create_database_engine", lambda _: engine)
    monkeypatch.setattr(worker_module, "_default_runtime_bindings", fail_bindings)
    monkeypatch.setattr(engine, "dispose", dispose)

    with pytest.raises(RuntimeError, match="synthetic dependency setup failure"):
        PaperWorker.from_settings(settings)

    assert dispose_calls == 1


def test_strategy_health_aggregates_structured_trace_events(tmp_path) -> None:
    store = _worker(tmp_path).store
    first = CycleOutcome(
        cycle_id="cycle:strategy-health-1",
        decision_key="decision:strategy-health-1",
        action="NO_TRADE",
        health=WorkerHealth.HEALTHY,
        data_status="HEALTHY",
        data_reason=None,
        eligible_strategy_count=1,
        reconciliation_status="HEALTHY",
        reason_codes=("FUSION_FLAT",),
        trace=(
            DecisionTraceEvent(
                "strategy_decision",
                "HEALTHY",
                metadata=(
                    ("strategy_id", "alpha"),
                    ("strategy_version", "1"),
                    ("direction", "long"),
                    ("intent_count", "2"),
                ),
            ),
        ),
    )
    second = replace(
        first,
        cycle_id="cycle:strategy-health-2",
        decision_key="decision:strategy-health-2",
        trace=(
            DecisionTraceEvent(
                "strategy_decision",
                "HEALTHY",
                metadata=(
                    ("strategy_id", "alpha"),
                    ("strategy_version", "1"),
                    ("direction", "hold"),
                    ("intent_count", "0"),
                ),
            ),
            DecisionTraceEvent(
                "strategy_decision",
                "HEALTHY",
                metadata=(
                    ("strategy_id", "beta"),
                    ("strategy_version", "2"),
                    ("direction", "short"),
                    ("intent_count", "1"),
                ),
            ),
            DecisionTraceEvent(
                "strategy_decision",
                "HEALTHY",
                metadata=(
                    ("strategy_id", "malformed"),
                    ("strategy_version", "1"),
                    ("direction", "diagonal"),
                    ("intent_count", "1"),
                ),
            ),
        ),
    )
    store.record_cycle(
        workload_id="default",
        decision_key=first.decision_key,
        started_at=datetime(2026, 1, 1, 12, tzinfo=UTC),
        completed_at=datetime(2026, 1, 1, 12, 1, tzinfo=UTC),
        outcome=first,
    )
    store.record_cycle(
        workload_id="default",
        decision_key=second.decision_key,
        started_at=datetime(2026, 1, 1, 12, 2, tzinfo=UTC),
        completed_at=datetime(2026, 1, 1, 12, 3, tzinfo=UTC),
        outcome=second,
    )

    assert store.strategy_health("default") == (
        StrategyHealthMetric(
            strategy_id="alpha",
            strategy_version="1",
            decision_count=2,
            long_signal_count=1,
            short_signal_count=0,
            flat_signal_count=0,
            hold_signal_count=1,
            intent_count=2,
            intent_decision_count=1,
        ),
        StrategyHealthMetric(
            strategy_id="beta",
            strategy_version="2",
            decision_count=1,
            long_signal_count=0,
            short_signal_count=1,
            flat_signal_count=0,
            hold_signal_count=0,
            intent_count=1,
            intent_decision_count=1,
        ),
    )
    bounded = store.strategy_health_report("default", cycle_limit=1)
    assert bounded.cycles_considered == 1
    assert bounded.truncated is True
    assert bounded.structured_decision_count == 2
    assert bounded.unattributed_decision_count == 1
    assert {item.strategy_id for item in bounded.metrics} == {"alpha", "beta"}


def test_strategy_health_tie_breaks_equal_completion_times_deterministically(tmp_path) -> None:
    store = _worker(tmp_path).store
    completed = datetime(2026, 1, 1, 12, 1, tzinfo=UTC)

    def outcome(cycle_id: str, strategy_id: str) -> CycleOutcome:
        return CycleOutcome(
            cycle_id=cycle_id,
            decision_key=f"decision:{cycle_id}",
            action="NO_TRADE",
            health=WorkerHealth.HEALTHY,
            data_status="HEALTHY",
            data_reason=None,
            eligible_strategy_count=1,
            reconciliation_status="HEALTHY",
            reason_codes=("FUSION_FLAT",),
            trace=(
                DecisionTraceEvent(
                    "strategy_decision",
                    "HEALTHY",
                    metadata=(
                        ("strategy_id", strategy_id),
                        ("strategy_version", "1"),
                        ("direction", "flat"),
                        ("intent_count", "0"),
                    ),
                ),
            ),
        )

    store.record_cycle(
        workload_id="default",
        decision_key="decision:older",
        started_at=completed - timedelta(minutes=1),
        completed_at=completed,
        outcome=outcome("cycle:older", "older"),
    )
    store.record_cycle(
        workload_id="default",
        decision_key="decision:newer",
        started_at=completed,
        completed_at=completed,
        outcome=outcome("cycle:newer", "newer"),
    )

    report = store.strategy_health_report("default", cycle_limit=1)

    assert report.truncated is True
    assert report.structured_decision_count == 1
    assert tuple(item.strategy_id for item in report.metrics) == ("newer",)


def test_strategy_health_does_not_count_replayed_strategy_events(tmp_path) -> None:
    store = _worker(tmp_path).store
    outcome = CycleOutcome(
        cycle_id="cycle:strategy-health-replay-1",
        decision_key="decision:strategy-health-replay",
        action="NO_TRADE",
        health=WorkerHealth.HEALTHY,
        data_status="HEALTHY",
        data_reason=None,
        eligible_strategy_count=1,
        reconciliation_status="HEALTHY",
        reason_codes=("FUSION_FLAT",),
        trace=(
            DecisionTraceEvent(
                "strategy_decision",
                "HEALTHY",
                metadata=(
                    ("strategy_id", "alpha"),
                    ("strategy_version", "1"),
                    ("direction", "flat"),
                    ("intent_count", "0"),
                ),
            ),
        ),
    )
    first = store.record_cycle(
        workload_id="default",
        decision_key=outcome.decision_key,
        started_at=datetime(2026, 1, 1, 12, tzinfo=UTC),
        completed_at=datetime(2026, 1, 1, 12, 1, tzinfo=UTC),
        outcome=outcome,
    )
    replay = store.record_cycle(
        workload_id="default",
        decision_key=outcome.decision_key,
        started_at=datetime(2026, 1, 1, 12, 2, tzinfo=UTC),
        completed_at=datetime(2026, 1, 1, 12, 3, tzinfo=UTC),
        outcome=replace(outcome, cycle_id="cycle:strategy-health-replay-2"),
    )

    assert first.replayed is False
    assert replay.replayed is True
    report = store.strategy_health_report("default")

    assert report.cycles_considered == 2
    assert report.replayed_cycle_count == 1
    assert report.structured_decision_count == 1
    assert report.metrics == (
        StrategyHealthMetric(
            strategy_id="alpha",
            strategy_version="1",
            decision_count=1,
            long_signal_count=0,
            short_signal_count=0,
            flat_signal_count=1,
            hold_signal_count=0,
            intent_count=0,
            intent_decision_count=0,
        ),
    )


def test_strategy_health_rendering_is_read_only_and_machine_safe(capsys) -> None:
    _print_strategy_health(
        StrategyHealthReport(
            workload_id="default",
            cycle_limit=1000,
            cycles_considered=1,
            truncated=False,
            replayed_cycle_count=0,
            structured_decision_count=1,
            unattributed_decision_count=0,
            metrics=(
                StrategyHealthMetric(
                    strategy_id="alpha",
                    strategy_version="1",
                    decision_count=2,
                    long_signal_count=1,
                    short_signal_count=0,
                    flat_signal_count=0,
                    hold_signal_count=1,
                    intent_count=2,
                    intent_decision_count=1,
                ),
            ),
        ),
        json_output=True,
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "cycle_limit": 1000,
        "cycles_considered": 1,
        "replayed_cycle_count": 0,
        "structured_decision_count": 1,
        "unattributed_decision_count": 0,
        "truncated": False,
        "workload_id": "default",
        "strategies": [
            {
                "strategy_id": "alpha",
                "strategy_version": "1",
                "decision_count": 2,
                "long_signal_count": 1,
                "short_signal_count": 0,
                "flat_signal_count": 0,
                "hold_signal_count": 1,
                "intent_count": 2,
                "intent_decision_count": 1,
            }
        ],
    }


def test_dataset_qualification_is_required_before_worker_activity(tmp_path) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    worker, _ = _configured_dataset_worker(tmp_path, now)

    status = worker.run_once()

    assert status.data_reason == "DATA_QUALIFICATION_NOT_CONFIGURED"
    assert status.last_decision == "NO_TRADE"
    assert status.paper_order_count == 0


def test_dataset_qualification_dataset_id_is_required(tmp_path) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    worker, _ = _configured_dataset_worker(tmp_path, now, qualification=True)
    worker.settings.worker_dataset_id = None

    status = worker.run_once()

    assert status.data_reason == "DATA_QUALIFICATION_DATASET_ID_NOT_CONFIGURED"
    assert status.last_decision == "NO_TRADE"


def test_dataset_qualification_dataset_id_must_match(tmp_path) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    worker, _ = _configured_dataset_worker(tmp_path, now, qualification=True)
    worker.settings.worker_dataset_id = "different-dataset"

    status = worker.run_once()

    assert status.data_reason == "DATA_QUALIFICATION_DATASET_ID_MISMATCH"
    assert status.last_decision == "NO_TRADE"
    assert status.paper_order_count == 0


def test_future_dated_dataset_qualification_blocks_worker_activity(tmp_path) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    worker, content_hash = _configured_dataset_worker(tmp_path, now, qualification=True)
    future = _write_test_qualification(tmp_path, content_hash, now + timedelta(hours=1))
    worker.settings.worker_dataset_qualification_id = future.qualification_id

    status = worker.run_once()

    assert status.data_reason == "DATA_QUALIFICATION_FUTURE_DATED"
    assert status.last_decision == "NO_TRADE"
    assert status.paper_order_count == 0


def test_future_availability_on_historical_bar_blocks_worker_activity(tmp_path) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    worker, _ = _configured_dataset_worker(tmp_path, now, qualification=True)
    original = worker.dependencies.market_store.query_bars(
        "EUR/USD",
        Timeframe.H1,
        now - timedelta(hours=2),
        now,
        source="local",
        adjustment_policy=AdjustmentPolicy.RAW,
    )[0]
    worker.dependencies.market_store.upsert_bars(
        (
            original.model_copy(
                update={
                    "timestamp": now - timedelta(hours=2),
                    "ingestion_timestamp": now + timedelta(minutes=1),
                    "quote_timestamp": now,
                }
            ),
        )
    )

    status = worker.run_once()

    assert status.data_reason == "DATA_FUTURE_DATED"
    assert status.last_decision == "NO_TRADE"
    assert status.paper_order_count == 0


def test_dataset_qualification_content_hash_must_match_persisted_bars(tmp_path) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    worker, _ = _configured_dataset_worker(
        tmp_path, now, qualification=True, qualification_hash="wrong-qualification-hash"
    )

    status = worker.run_once()

    assert status.data_reason == "DATA_QUALIFICATION_DATASET_MISMATCH"
    assert status.last_decision == "NO_TRADE"
    assert status.paper_order_count == 0


def test_rejected_dataset_qualification_blocks_worker_activity(tmp_path) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    worker, _ = _configured_dataset_worker(
        tmp_path, now, qualification=True, qualification_verdict=DatasetQualificationStatus.REJECTED
    )

    status = worker.run_once()

    assert status.data_reason == "DATA_QUALIFICATION_REJECTED"
    assert status.last_decision == "NO_TRADE"
    assert status.paper_order_count == 0


def test_quality_error_from_other_source_does_not_block_admitted_source(tmp_path) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    worker, _ = _configured_dataset_worker(tmp_path, now, qualification=True)
    worker.dependencies.market_store.record_quality_events(
        (
            DataQualityEvent(
                occurred_at=now,
                severity=QualitySeverity.ERROR,
                code=QualityCode.INVALID_PRICE,
                message="unrelated provider error",
                source="other-provider",
                symbol="EUR/USD",
                timeframe="1h",
                bar_timestamp=now - timedelta(hours=1),
            ),
        )
    )

    status = worker.run_once()

    assert status.data_status == "HEALTHY"
    assert status.data_reason is None
    assert status.last_decision == "NO_TRADE"


def test_quality_error_for_admitted_source_blocks_activity(tmp_path) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    worker, _ = _configured_dataset_worker(tmp_path, now, qualification=True)
    worker.dependencies.market_store.record_quality_events(
        (
            DataQualityEvent(
                occurred_at=now,
                severity=QualitySeverity.ERROR,
                code=QualityCode.INVALID_PRICE,
                message="configured provider error",
                source="local",
                symbol="EUR/USD",
                timeframe="1h",
                bar_timestamp=now - timedelta(hours=1),
            ),
        )
    )

    status = worker.run_once()

    assert status.data_reason == "DATA_QUALITY_ERROR"
    assert status.last_decision == "NO_TRADE"
    assert status.paper_order_count == 0


def test_global_quality_error_blocks_admitted_source(tmp_path) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    worker, _ = _configured_dataset_worker(tmp_path, now, qualification=True)
    worker.dependencies.market_store.record_quality_events(
        (
            DataQualityEvent(
                occurred_at=now,
                severity=QualitySeverity.ERROR,
                code=QualityCode.INVALID_PRICE,
                message="global provider quality error",
            ),
        )
    )

    status = worker.run_once()

    assert status.data_reason == "DATA_QUALITY_ERROR"
    assert status.last_decision == "NO_TRADE"
    assert status.paper_order_count == 0


def test_future_quality_error_does_not_block_current_admission(tmp_path) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    worker, _ = _configured_dataset_worker(tmp_path, now, qualification=True)
    worker.dependencies.market_store.record_quality_events(
        (
            DataQualityEvent(
                occurred_at=now + timedelta(hours=1),
                severity=QualitySeverity.ERROR,
                code=QualityCode.INVALID_PRICE,
                message="future quality finding",
                source="local",
                symbol="EUR/USD",
                timeframe="1h",
                bar_timestamp=now + timedelta(hours=1),
            ),
        )
    )

    status = worker.run_once()

    assert status.data_status == "HEALTHY"
    assert status.data_reason is None
    assert status.last_decision == "NO_TRADE"


def test_market_store_failure_during_admission_is_failed_not_data_invalid(
    tmp_path, monkeypatch
) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    worker, _ = _configured_dataset_worker(tmp_path, now, qualification=True)

    def fail_query(*_: object, **__: object) -> tuple[MarketBar, ...]:
        raise SQLAlchemyError("synthetic market-store failure")

    monkeypatch.setattr(worker.dependencies.market_store, "query_bars", fail_query)

    status = worker.run_once()

    assert status.health is WorkerHealth.FAILED
    assert status.data_status == "UNKNOWN"
    assert status.data_reason == "DATA_NOT_EVALUATED"
    assert status.last_error == "synthetic market-store failure"
    assert "WORKER_CYCLE_FAILED" in status.last_reason_codes
    assert status.paper_order_count == 0


def test_invalid_bars_during_admission_remain_blocked_as_data_invalid(
    tmp_path, monkeypatch
) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    worker, _ = _configured_dataset_worker(tmp_path, now, qualification=True)

    def reject_bars(_: object) -> None:
        raise DataValidationError("synthetic invalid market bars")

    monkeypatch.setattr("traderos.data.validation.validate_batch", reject_bars)

    status = worker.run_once()

    assert status.health is WorkerHealth.BLOCKED
    assert status.data_status == "BLOCKED"
    assert status.data_reason == "DATA_INVALID"
    assert status.last_decision == "NO_TRADE"
    assert status.paper_order_count == 0


def test_ready_probe_requires_healthy_running_paper_worker(tmp_path, monkeypatch, capsys) -> None:
    worker = _worker(tmp_path)
    blocked = worker.run_once()

    assert not _is_ready(blocked)
    monkeypatch.setenv("TRADING_MODE", "paper")
    result = main(["ready", "--database-url", f"sqlite:///{tmp_path / 'worker.db'}"])
    output = capsys.readouterr().out

    assert result == 2
    assert "HEALTH=BLOCKED" in output
    healthy = replace(
        blocked,
        worker_state=WorkerState.RUNNING,
        health=WorkerHealth.HEALTHY,
        mode="PAPER",
        data_status="HEALTHY",
        data_reason=None,
        reconciliation_status="HEALTHY",
        stop_requested=False,
        last_error=None,
    )
    assert _is_ready(healthy)
    assert not _is_ready(replace(healthy, data_status="BLOCKED"))
    assert not _is_ready(replace(healthy, reconciliation_status="FAILED"))
    assert not _is_ready(replace(healthy, last_error="stale failure"))
    assert not _is_ready(replace(healthy, stop_requested=True))


def test_worker_run_exit_code_distinguishes_safe_block_from_failure(tmp_path) -> None:
    blocked = _worker(tmp_path).run_once()

    assert _worker_run_exit_code(blocked) == 0
    assert _worker_run_exit_code(replace(blocked, health=WorkerHealth.FAILED)) == 1
    assert _worker_run_exit_code(replace(blocked, health=WorkerHealth.UNKNOWN)) == 1
    assert _worker_run_exit_code(replace(blocked, last_decision="ORDER_SUBMITTED")) == 1
    assert _worker_run_exit_code(replace(blocked, last_error="stale failure")) == 1
    assert _worker_run_exit_code(replace(blocked, mode="LIVE")) == 1


def test_cli_status_fails_cleanly_when_persistence_schema_is_unavailable(
    tmp_path, capsys
) -> None:
    database_url = f"sqlite:///{tmp_path / 'missing' / 'worker.db'}"

    result = main(["status", "--database-url", database_url])
    output = capsys.readouterr().out

    assert result == 1
    assert output == "FAILED: worker persistence schema is unavailable\n"


def test_startup_persistence_failure_releases_acquired_lease(tmp_path, monkeypatch) -> None:
    worker = _worker(tmp_path)

    def fail_status(_: str) -> WorkerStatus:
        raise SQLAlchemyError("synthetic status failure")

    monkeypatch.setattr(worker.store, "status", fail_status)

    with pytest.raises(WorkerError, match="worker lease persistence is unavailable"):
        worker.run_once()

    assert worker.store.lease_claimant(
        "default", now=datetime(2026, 1, 1, 12, tzinfo=UTC)
    ) is None


def test_existing_worker_schema_gets_data_latest_column(tmp_path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'legacy-worker.db'}")
    store = WorkerStore(engine)
    store.create_schema()
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE worker_states DROP COLUMN data_latest_at"))

    store.create_schema()

    assert "data_latest_at" in {
        item["name"] for item in inspect(engine).get_columns("worker_states")
    }


def test_existing_worker_schema_gets_lease_claim_column(tmp_path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'legacy-lease.db'}")
    store = WorkerStore(engine)
    store.create_schema()
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE worker_leases DROP COLUMN claimed_workload_id"))

    store.create_schema()

    assert "claimed_workload_id" in {
        item["name"] for item in inspect(engine).get_columns("worker_leases")
    }


def test_postgres_worker_requires_numbered_schema_migrations(monkeypatch) -> None:
    engine = cast(Engine, SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
    inspector = SimpleNamespace(get_table_names=lambda: ())
    monkeypatch.setattr("traderos.worker.store.inspect", lambda _: inspector)

    with pytest.raises(ValueError, match="apply numbered migrations"):
        WorkerStore(engine).create_schema()


def test_postgres_worker_accepts_complete_schema_without_create_all(monkeypatch) -> None:
    engine = cast(Engine, SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
    inspector = SimpleNamespace(
        get_table_names=lambda: list(metadata.tables),
        get_columns=lambda table_name: [
            {"name": column.name, "nullable": column.nullable}
            for column in metadata.tables[table_name].columns
        ],
    )
    monkeypatch.setattr("traderos.worker.store.inspect", lambda _: inspector)

    def unexpected_create_all(*_: object, **__: object) -> None:
        raise AssertionError("PostgreSQL startup must not call create_all")

    monkeypatch.setattr(metadata, "create_all", unexpected_create_all)
    WorkerStore(engine).create_schema()


def test_postgres_worker_rejects_nullable_lease_claim_column(monkeypatch) -> None:
    engine = cast(Engine, SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
    inspector = SimpleNamespace(
        get_table_names=lambda: list(metadata.tables),
        get_columns=lambda table_name: [
            {
                "name": column.name,
                "nullable": (
                    True
                    if table_name == "worker_leases" and column.name == "claimed_workload_id"
                    else column.nullable
                ),
            }
            for column in metadata.tables[table_name].columns
        ],
    )
    monkeypatch.setattr("traderos.worker.store.inspect", lambda _: inspector)

    with pytest.raises(ValueError, match="nullable columns=worker_leases"):
        WorkerStore(engine).create_schema()


def test_default_worker_wires_explicit_local_health_checks(tmp_path) -> None:
    worker = _worker(tmp_path)
    provider = worker.dependencies.system_health_provider
    assert provider is not None

    snapshot = provider(
        datetime(2026, 1, 1, tzinfo=UTC),
        (HealthCheck("persistence", True), HealthCheck("lease", True)),
    )

    assert snapshot.status is SystemHealthStatus.HEALTHY


def test_default_worker_wires_dataset_qualification_registry(tmp_path) -> None:
    root = tmp_path / "qualifications"
    worker = _worker(
        tmp_path,
        worker_dataset_qualification_id="missing-qualification",
        worker_dataset_qualification_root=str(root),
    )

    registry = worker.dependencies.dataset_qualification_registry
    assert registry is not None
    assert registry.root == root


def test_restart_does_not_duplicate_cycle_effects_or_orders(tmp_path) -> None:
    worker = _worker(tmp_path)

    first = worker.run_once()
    second = worker.run_once()

    assert first.last_decision == second.last_decision == "NO_TRADE"
    assert worker.store.cycle_count("default") == 2
    assert second.last_trace[-1].stage == "idempotency"
    assert second.last_trace[-1].reason == "DUPLICATE_DECISION"
    assert worker.dependencies.paper_engine.store.orders("missing") == ()


def test_restart_reconciliation_blocks_corrupted_pending_reservation(tmp_path) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    worker = _worker(tmp_path, worker_account_id="paper-account")
    firewall = worker.dependencies.pipeline.risk_firewall  # type: ignore[union-attr]
    worker.dependencies.paper_engine.create_account(
        account_id="paper-account",
        account_currency="USD",
        starting_cash=Decimal("10000"),
        risk_capacity=Decimal("10000"),
        daily_loss_limit=firewall.parameters.daily_loss_limit,
        max_drawdown=firewall.parameters.max_drawdown,
        risk_policy_id=firewall.policy_id,
        risk_policy_configuration_id=firewall.configuration_id,
        timestamp=now,
    )
    instrument = Instrument(
        canonical_symbol="EUR/USD",
        asset_class=AssetClass.FOREX,
        base_currency="EUR",
        quote_currency="USD",
    )
    decision = RiskDecision(
        decision_id="pending",
        status=RiskDecisionStatus.APPROVE,
        decision_timestamp=now,
        intent_id="reconcile-intent",
        account_id="paper-account",
        instrument=instrument,
        direction=FusionDirection.LONG,
        policy_id=firewall.policy_id,
        policy_version="1",
        configuration_id=firewall.configuration_id,
        reason_codes=(),
        checks=(),
        authorization=RiskAuthorization(
            action=RiskAction.NEW_OR_INCREASE,
            max_new_notional=Decimal("500"),
            max_loss_at_stop=Decimal("50"),
            max_reduction_notional=Decimal("0"),
        ),
        regime_state_id="state",
        portfolio_snapshot_timestamp=now,
        market_snapshot_timestamp=now,
    )
    decision = replace(decision, decision_id=risk_decision_integrity_id(decision))
    order = worker.dependencies.paper_engine.submit(
        account_id="paper-account",
        idempotency_key="reconcile-pending",
        risk_decision=decision,
        quote=PaperQuote(instrument, now, Decimal("1"), Decimal("1.01")),
        timestamp=now,
    )
    with worker.dependencies.paper_engine.store.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE paper_reservations SET active = 0, released_at = :released "
                "WHERE order_id = :order_id"
            ),
            {"released": now + timedelta(seconds=1), "order_id": order.order_id},
        )

    restarted = _worker(tmp_path, worker_account_id="paper-account")
    restarted._clock = lambda: now
    status = restarted.run_once()

    assert status.health is WorkerHealth.FAILED
    assert status.reconciliation_status == "FAILED"
    assert "RECONCILIATION_FAILED" in status.last_reason_codes
    assert status.last_decision == "NO_TRADE"
    assert restarted.store.cycle_count("default") == 1


def test_stop_request_preserves_newer_worker_projection(tmp_path) -> None:
    worker = _worker(tmp_path)

    before = worker.run_once()
    assert before.last_cycle_at is not None
    worker.store.set_stop_requested(
        "default", True, now=before.last_cycle_at + timedelta(seconds=1)
    )

    after = worker.store.status("default")

    assert after.stop_requested is True
    assert after.worker_state is WorkerState.STOPPED
    assert after.last_cycle_at == before.last_cycle_at
    assert after.last_decision == before.last_decision
    assert after.last_reason_codes == before.last_reason_codes
    assert after.last_trace == before.last_trace


def test_lease_owned_status_write_preserves_concurrent_stop_request(tmp_path) -> None:
    worker = _worker(tmp_path)
    worker._begin(reset_stop=True)
    try:
        now = datetime(2026, 1, 1, 12, tzinfo=UTC)
        worker.store.set_stop_requested("default", True, now=now)
        current = worker.store.status("default")
        assert current.stop_requested is True
        assert worker._save_owned_status(
            replace(current, worker_state=WorkerState.RUNNING, stop_requested=False),
            updated_at=now + timedelta(seconds=1),
        )
        preserved = worker.store.status("default")
        assert preserved.stop_requested is True
        assert preserved.worker_state is WorkerState.STOPPING
    finally:
        worker._finish()


def test_stop_request_creates_control_state_for_unknown_workload(tmp_path) -> None:
    worker = _worker(tmp_path)
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)

    worker.store.set_stop_requested("new-workload", True, now=now)

    status = worker.store.status("new-workload")
    assert status.stop_requested is True
    assert status.worker_state is WorkerState.UNKNOWN
    assert status.last_decision == "UNKNOWN"


@pytest.mark.parametrize("pipeline_health", ("HEALTHY", "BLOCKED", "FAILED"))
def test_restart_does_not_reprocess_completed_actionable_decision(
    tmp_path, pipeline_health: str
) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    worker = _worker(
        tmp_path,
        worker_account_id="paper-account",
        worker_instrument="EUR/USD",
        worker_timeframe="1h",
        worker_data_source="local",
        worker_dataset_version="qualified-actionable",
        worker_dataset_id="software-test-dataset",
        worker_strategy_versions="fixture_strategy@1",
    )
    firewall = worker.dependencies.pipeline.risk_firewall  # type: ignore[union-attr]
    worker.dependencies.paper_engine.create_account(
        account_id="paper-account",
        account_currency="USD",
        starting_cash=Decimal("10000"),
        risk_capacity=Decimal("10000"),
        daily_loss_limit=firewall.parameters.daily_loss_limit,
        max_drawdown=firewall.parameters.max_drawdown,
        risk_policy_id=firewall.policy_id,
        risk_policy_configuration_id=firewall.configuration_id,
        timestamp=now,
    )
    bar = MarketBar(
        instrument=Instrument(
            canonical_symbol="EUR/USD",
            asset_class=AssetClass.FOREX,
            base_currency="EUR",
            quote_currency="USD",
        ),
        timeframe=Timeframe.H1,
        timestamp=now - timedelta(hours=1),
        open=Decimal("1.10"),
        high=Decimal("1.11"),
        low=Decimal("1.09"),
        close=Decimal("1.105"),
        source="local",
        ingestion_timestamp=now,
        bid=Decimal("1.104"),
        ask=Decimal("1.106"),
        quote_timestamp=now,
    )
    worker.dependencies.market_store.upsert_bars((bar,))
    dataset_hash = market_bar_content_hash(
        worker.dependencies.market_store.query_bars(
            "EUR/USD",
            Timeframe.H1,
            bar.timestamp,
            now,
            source="local",
            adjustment_policy=AdjustmentPolicy.RAW,
        )
    )
    worker.dependencies.market_store.record_dataset(
        DatasetMetadata(
            dataset_version="qualified-actionable",
            dataset_hash=dataset_hash,
            provider="local",
            symbol="EUR/USD",
            timeframe="1h",
            start=bar.timestamp,
            end=now,
            adjustment_policy=AdjustmentPolicy.RAW,
            configuration_version="test",
            quality_status="clean",
            created_at=now,
        )
    )
    qualification = _write_test_qualification(tmp_path, dataset_hash, now)
    worker.settings.worker_dataset_qualification_id = qualification.qualification_id
    qualification_registry = DatasetQualificationRegistry(tmp_path / "qualifications")

    class SpyPipeline:
        def __init__(self, health: str) -> None:
            self.calls = 0
            self.health = health

        def run(self, **_: object) -> PaperPipelineOutcome:
            self.calls += 1
            return PaperPipelineOutcome(
                action="ORDER_SUBMITTED",
                health=self.health,
                reason_codes=("ORDER_SUBMITTED",),
                trace=(PipelineTraceEvent("paper_submission", "HEALTHY", "fixture"),),
            )

    spy = SpyPipeline(pipeline_health)
    worker.dependencies = WorkerDependencies(
        market_store=worker.dependencies.market_store,
        paper_engine=worker.dependencies.paper_engine,
        governance=SimpleNamespace(
            explain_strategy_eligibility=lambda strategy_id, version: SimpleNamespace(
                operationally_eligible=True,
                reasons=(),
            )
        ),
            pipeline=cast("PaperDecisionPipeline", spy),
            dataset_qualification_registry=qualification_registry,
            system_health_provider=worker.dependencies.system_health_provider,
    )
    worker._clock = lambda: now

    first = worker.run_once()
    second = worker.run_once()

    assert first.last_decision == "ORDER_SUBMITTED"
    assert first.health.value == pipeline_health
    expected_second_action = (
        "ORDER_SUBMITTED" if pipeline_health == "FAILED" else "NO_TRADE"
    )
    assert second.last_decision == expected_second_action
    assert second.health.value == pipeline_health
    assert spy.calls == (2 if pipeline_health == "FAILED" else 1)
    assert any(event.stage == "idempotency" for event in second.last_trace) is (
        pipeline_health != "FAILED"
    )
    assert ("DUPLICATE_DECISION" in second.last_reason_codes) is (pipeline_health != "FAILED")


@pytest.mark.parametrize("pipeline_health", ("HEALTHY", "FAILED"))
def test_risk_lock_change_reopens_same_market_decision(
    tmp_path, monkeypatch, pipeline_health: str
) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    worker = _worker(
        tmp_path,
        worker_account_id="paper-account",
        worker_instrument="EUR/USD",
        worker_timeframe="1h",
        worker_dataset_version="fixture-lock-state",
        worker_strategy_versions="fixture_strategy@1",
    )
    bar = MarketBar(
        instrument=Instrument(
            canonical_symbol="EUR/USD",
            asset_class=AssetClass.FOREX,
            base_currency="EUR",
            quote_currency="USD",
        ),
        timeframe=Timeframe.H1,
        timestamp=now - timedelta(hours=1),
        open=Decimal("1.10"),
        high=Decimal("1.11"),
        low=Decimal("1.09"),
        close=Decimal("1.105"),
        source="local",
        ingestion_timestamp=now,
        bid=Decimal("1.104"),
        ask=Decimal("1.106"),
        quote_timestamp=now,
    )
    market_store = SimpleNamespace(
        latest_bar=lambda *args, **kwargs: bar,
        query_bars=lambda *args, **kwargs: (bar,),
        quality_events=lambda: (),
    )
    lock_states = iter(("manual", "", "", ""))
    monkeypatch.setattr(
        worker,
        "_reconcile",
        lambda: (
            "HEALTHY",
            tuple(lock for lock in (next(lock_states),) if lock),
            0,
            0,
        ),
    )
    monkeypatch.setattr(
        worker.dependencies.paper_engine.store,
        "active_locks",
        lambda _: {
            SimpleNamespace(value=lock)
            for lock in (next(lock_states),)
            if lock
        },
    )
    monkeypatch.setattr(
        worker,
        "_admit_data",
        lambda _: DataAdmission("HEALTHY", None, bar.timestamp, "fixture", "hash"),
    )
    monkeypatch.setattr(worker, "_resolve_strategy_eligibility", lambda: (1, ()))

    class SpyPipeline:
        calls = 0

        def run(self, **_: object) -> PaperPipelineOutcome:
            self.calls += 1
            return PaperPipelineOutcome(
                action="NO_TRADE",
                health=pipeline_health,
                reason_codes=("FIXTURE_NO_TRADE",),
                trace=(),
            )

    spy = SpyPipeline()
    worker.dependencies = WorkerDependencies(
        market_store=cast("MarketDataStore", market_store),
        paper_engine=worker.dependencies.paper_engine,
        governance=worker.dependencies.governance,
        pipeline=cast("PaperDecisionPipeline", spy),
        system_health_provider=worker.dependencies.system_health_provider,
    )
    worker._clock = lambda: now

    first = worker.run_once()
    second = worker.run_once()

    assert first.last_decision == second.last_decision == "NO_TRADE"
    assert first.health.value == second.health.value == pipeline_health
    assert spy.calls == 2
    assert not any(event.stage == "idempotency" for event in second.last_trace)


def test_decision_identity_includes_system_health_state(tmp_path) -> None:
    worker = _worker(tmp_path)
    common = {
        "data": DataAdmission(
            "HEALTHY",
            None,
            datetime(2026, 1, 1, 11, tzinfo=UTC),
            "fixture",
            "hash",
        ),
        "eligible_count": 1,
        "reconciliation_status": "HEALTHY",
        "active_locks": (),
        "strategy_reasons": (),
        "gate_state": "OPEN",
    }

    unavailable = worker._decision_key(**common, system_health_status="unavailable")
    healthy = worker._decision_key(**common, system_health_status="healthy")

    assert unavailable != healthy


def test_decision_identity_includes_effective_policy_configuration(tmp_path) -> None:
    worker = _worker(tmp_path)
    common = {
        "data": DataAdmission(
            "HEALTHY",
            None,
            datetime(2026, 1, 1, 11, tzinfo=UTC),
            "fixture",
            "hash",
        ),
        "eligible_count": 1,
        "reconciliation_status": "HEALTHY",
        "active_locks": (),
        "strategy_reasons": (),
        "system_health_status": "healthy",
        "gate_state": "OPEN",
    }

    baseline = worker._decision_key(**common)
    assert worker.dependencies.pipeline is not None
    pipeline = worker.dependencies.pipeline
    pipeline.risk_firewall = RiskFirewall(
        pipeline.risk_firewall.parameters.model_copy(
            update={"max_spread_fraction": Decimal("0.00001")}
        )
    )

    changed = worker._decision_key(**common)

    assert baseline != changed

    pipeline._bindings[  # type: ignore[attr-defined]
        ("forex_trend_following", "1")
    ] = replace(
        next(iter(pipeline._bindings.values())),  # type: ignore[attr-defined]
        code_identity="changed-runtime-code",
    )
    runtime_changed = worker._decision_key(**common)

    assert changed != runtime_changed


def test_system_health_recovery_reopens_blocked_decision(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    worker = _worker(
        tmp_path,
        worker_account_id="paper-account",
        worker_instrument="EUR/USD",
        worker_timeframe="1h",
        worker_dataset_version="fixture-health-state",
        worker_strategy_versions="fixture_strategy@1",
    )
    bar = MarketBar(
        instrument=Instrument(
            canonical_symbol="EUR/USD",
            asset_class=AssetClass.FOREX,
            base_currency="EUR",
            quote_currency="USD",
        ),
        timeframe=Timeframe.H1,
        timestamp=now - timedelta(hours=1),
        open=Decimal("1.10"),
        high=Decimal("1.11"),
        low=Decimal("1.09"),
        close=Decimal("1.105"),
        source="local",
        ingestion_timestamp=now,
        bid=Decimal("1.104"),
        ask=Decimal("1.106"),
        quote_timestamp=now,
    )
    market_store = SimpleNamespace(
        latest_bar=lambda *args, **kwargs: bar,
        query_bars=lambda *args, **kwargs: (bar,),
        quality_events=lambda: (),
    )
    health_states = iter((SystemHealthStatus.UNAVAILABLE, SystemHealthStatus.HEALTHY))
    monkeypatch.setattr(worker, "_reconcile", lambda: ("HEALTHY", (), 0, 0))
    monkeypatch.setattr(
        worker,
        "_admit_data",
        lambda _: DataAdmission("HEALTHY", None, bar.timestamp, "fixture", "hash"),
    )
    monkeypatch.setattr(worker, "_resolve_strategy_eligibility", lambda: (1, ()))

    class HealthRecoveryPipeline:
        calls = 0

        def run(self, **_: object) -> PaperPipelineOutcome:
            self.calls += 1
            return PaperPipelineOutcome(
                action="NO_TRADE",
                health="BLOCKED",
                reason_codes=("RISK_SYSTEM_UNHEALTHY",),
                trace=(),
            )

    pipeline = HealthRecoveryPipeline()

    def health_provider(
        timestamp: datetime, _: tuple[HealthCheck, ...]
    ) -> SystemHealthSnapshot:
        return SystemHealthSnapshot(timestamp, next(health_states))

    worker.dependencies = WorkerDependencies(
        market_store=cast("MarketDataStore", market_store),
        paper_engine=worker.dependencies.paper_engine,
        governance=worker.dependencies.governance,
        pipeline=cast("PaperDecisionPipeline", pipeline),
        system_health_provider=health_provider,
    )
    worker._clock = lambda: now

    first = worker.run_once()
    second = worker.run_once()

    assert first.health.value == second.health.value == "BLOCKED"
    assert pipeline.calls == 2
    assert not any(event.stage == "idempotency" for event in second.last_trace)


def test_latest_bar_change_after_admission_blocks_pipeline(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    worker = _worker(
        tmp_path,
        worker_account_id="paper-account",
        worker_instrument="EUR/USD",
        worker_timeframe="1h",
        worker_strategy_versions="fixture_strategy@1",
    )
    firewall = worker.dependencies.pipeline.risk_firewall  # type: ignore[union-attr]
    worker.dependencies.paper_engine.create_account(
        account_id="paper-account",
        account_currency="USD",
        starting_cash=Decimal("10000"),
        risk_capacity=Decimal("10000"),
        daily_loss_limit=firewall.parameters.daily_loss_limit,
        max_drawdown=firewall.parameters.max_drawdown,
        risk_policy_id=firewall.policy_id,
        risk_policy_configuration_id=firewall.configuration_id,
        timestamp=now,
    )
    bar = MarketBar(
        instrument=Instrument(
            canonical_symbol="EUR/USD",
            asset_class=AssetClass.FOREX,
            base_currency="EUR",
            quote_currency="USD",
        ),
        timeframe=Timeframe.H1,
        timestamp=now - timedelta(hours=1),
        open=Decimal("1.10"),
        high=Decimal("1.11"),
        low=Decimal("1.09"),
        close=Decimal("1.105"),
        source="local",
        ingestion_timestamp=now,
        bid=Decimal("1.104"),
        ask=Decimal("1.106"),
        quote_timestamp=now,
    )
    worker.dependencies.market_store.upsert_bars((bar,))
    original_latest = worker.dependencies.market_store.latest_bar
    new_bar = bar.model_copy(
        update={"timestamp": now, "ingestion_timestamp": now, "quote_timestamp": now}
    )
    latest_calls = 0

    def changing_latest(*args: object, **kwargs: object) -> MarketBar | None:
        nonlocal latest_calls
        latest_calls += 1
        if latest_calls == 1:
            worker.dependencies.market_store.upsert_bars((new_bar,))
        return original_latest(*args, **kwargs)

    monkeypatch.setattr(worker.dependencies.market_store, "latest_bar", changing_latest)
    monkeypatch.setattr(
        worker,
        "_admit_data",
        lambda _: DataAdmission(
            "HEALTHY", None, bar.timestamp, "qualified-actionable", "fixture-hash"
        ),
    )

    class SpyPipeline:
        calls = 0

        def run(self, **_: object) -> PaperPipelineOutcome:
            self.calls += 1
            return PaperPipelineOutcome(
                action="NO_TRADE",
                health="HEALTHY",
                reason_codes=("UNEXPECTED_PIPELINE_ENTRY",),
                trace=(),
            )

    spy = SpyPipeline()
    worker.dependencies = WorkerDependencies(
        market_store=worker.dependencies.market_store,
        paper_engine=worker.dependencies.paper_engine,
        governance=SimpleNamespace(
            explain_strategy_eligibility=lambda strategy_id, version: SimpleNamespace(
                operationally_eligible=True,
                reasons=(),
            )
        ),
        pipeline=cast("PaperDecisionPipeline", spy),
        system_health_provider=worker.dependencies.system_health_provider,
    )
    worker._clock = lambda: now

    status = worker.run_once()

    assert spy.calls == 0
    assert status.last_decision == "NO_TRADE"
    assert status.health.value == "BLOCKED"
    assert "DATA_CHANGED_DURING_CYCLE" in status.last_reason_codes
    assert worker.dependencies.paper_engine.store.orders("paper-account") == ()


def test_same_timestamp_data_recheck_failure_blocks_pipeline(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    worker = _worker(
        tmp_path,
        worker_account_id="paper-account",
        worker_instrument="EUR/USD",
        worker_timeframe="1h",
        worker_strategy_versions="fixture_strategy@1",
    )
    bar = MarketBar(
        instrument=Instrument(
            canonical_symbol="EUR/USD",
            asset_class=AssetClass.FOREX,
            base_currency="EUR",
            quote_currency="USD",
        ),
        timeframe=Timeframe.H1,
        timestamp=now - timedelta(hours=1),
        open=Decimal("1.10"),
        high=Decimal("1.11"),
        low=Decimal("1.09"),
        close=Decimal("1.105"),
        source="local",
        ingestion_timestamp=now,
        bid=Decimal("1.104"),
        ask=Decimal("1.106"),
        quote_timestamp=now,
    )
    market_store = SimpleNamespace(latest_bar=lambda *args, **kwargs: bar)
    admissions = iter(
        (
            DataAdmission("HEALTHY", None, bar.timestamp, "fixture", "hash-a"),
            DataAdmission(
                "BLOCKED", "DATA_IDENTITY_MISMATCH", bar.timestamp, "fixture", "hash-a"
            ),
        )
    )
    monkeypatch.setattr(worker, "_admit_data", lambda _: next(admissions))
    monkeypatch.setattr(worker, "_reconcile", lambda: ("HEALTHY", (), 0, 0))
    monkeypatch.setattr(worker, "_resolve_strategy_eligibility", lambda: (1, ()))
    pipeline_calls = 0

    class SpyPipeline:
        def run(self, **_: object) -> PaperPipelineOutcome:
            nonlocal pipeline_calls
            pipeline_calls += 1
            return PaperPipelineOutcome(
                action="ORDER_SUBMITTED",
                health="HEALTHY",
                reason_codes=("UNEXPECTED_PIPELINE_ENTRY",),
                trace=(),
            )

    worker.dependencies = WorkerDependencies(
        market_store=cast("MarketDataStore", market_store),
        paper_engine=worker.dependencies.paper_engine,
        governance=worker.dependencies.governance,
        pipeline=cast("PaperDecisionPipeline", SpyPipeline()),
        system_health_provider=worker.dependencies.system_health_provider,
    )
    worker._clock = lambda: now

    status = worker.run_once()

    assert pipeline_calls == 0
    assert status.data_status == "BLOCKED"
    assert status.data_reason == "DATA_CHANGED_DURING_CYCLE"
    assert status.last_decision == "NO_TRADE"
    assert "DATA_CHANGED_DURING_CYCLE" in status.last_reason_codes
    assert worker.dependencies.paper_engine.store.orders("paper-account") == ()


def test_execution_recheck_uses_current_time_not_cycle_start(tmp_path, monkeypatch) -> None:
    started = datetime(2026, 1, 1, 12, tzinfo=UTC)
    recheck_time = started + timedelta(seconds=10)
    execution_time = recheck_time + timedelta(seconds=1)
    completed = execution_time + timedelta(seconds=1)
    clock_values = iter((started, started, recheck_time, execution_time, completed, completed))

    worker = _worker(
        tmp_path,
        worker_account_id="paper-account",
        worker_instrument="EUR/USD",
        worker_timeframe="1h",
        worker_strategy_versions="fixture_strategy@1",
    )
    bar = MarketBar(
        instrument=Instrument(
            canonical_symbol="EUR/USD",
            asset_class=AssetClass.FOREX,
            base_currency="EUR",
            quote_currency="USD",
        ),
        timeframe=Timeframe.H1,
        timestamp=started - timedelta(hours=1),
        open=Decimal("1.10"),
        high=Decimal("1.11"),
        low=Decimal("1.09"),
        close=Decimal("1.105"),
        source="local",
        ingestion_timestamp=started,
        bid=Decimal("1.104"),
        ask=Decimal("1.106"),
        quote_timestamp=started,
    )
    market_store = SimpleNamespace(
        latest_bar=lambda *args, **kwargs: bar,
        query_bars=lambda *args, **kwargs: (bar,),
        quality_events=lambda: (),
    )
    admission_times: list[datetime] = []

    def admit_data(timestamp: datetime) -> DataAdmission:
        admission_times.append(timestamp)
        return DataAdmission("HEALTHY", None, bar.timestamp, "fixture", "hash-a")

    monkeypatch.setattr(worker, "_admit_data", admit_data)
    monkeypatch.setattr(worker, "_reconcile", lambda: ("HEALTHY", (), 0, 0))
    monkeypatch.setattr(worker, "_resolve_strategy_eligibility", lambda: (1, ()))
    pipeline_times: list[datetime] = []

    class SpyPipeline:
        def run(self, **kwargs: object) -> PaperPipelineOutcome:
            pipeline_times.append(cast(datetime, kwargs["now"]))
            return PaperPipelineOutcome(
                action="NO_TRADE",
                health="HEALTHY",
                reason_codes=("FIXTURE_NO_TRADE",),
                trace=(),
            )

    worker.dependencies = WorkerDependencies(
        market_store=cast("MarketDataStore", market_store),
        paper_engine=worker.dependencies.paper_engine,
        governance=worker.dependencies.governance,
        pipeline=cast("PaperDecisionPipeline", SpyPipeline()),
        system_health_provider=worker.dependencies.system_health_provider,
    )
    worker._clock = lambda: next(clock_values, completed)

    status = worker.run_once()

    assert admission_times == [started, recheck_time]
    assert pipeline_times == [execution_time]
    assert status.last_decision == "NO_TRADE"
    assert worker.dependencies.paper_engine.store.orders("paper-account") == ()


def test_disabled_strategy_is_not_eligible_and_cannot_create_orders(tmp_path) -> None:
    worker = _worker(
        tmp_path,
        worker_strategy_versions="fixture_strategy@1",
        worker_instrument="EUR/USD",
        worker_timeframe="1h",
    )
    worker.dependencies = WorkerDependencies(
        market_store=worker.dependencies.market_store,
        paper_engine=worker.dependencies.paper_engine,
        governance=SimpleNamespace(
            explain_strategy_eligibility=lambda strategy_id, version: SimpleNamespace(
                operationally_eligible=False,
                reasons=("STRATEGY_DISABLED",),
            )
        ),
    )  # type: ignore[arg-type]

    status = worker.run_once()

    assert status.eligible_strategy_count == 0
    assert "fixture_strategy@1:STRATEGY_DISABLED" in status.last_reason_codes
    assert status.last_decision == "NO_TRADE"


def test_real_governance_missing_artifact_is_not_eligible(tmp_path) -> None:
    worker = _worker(
        tmp_path,
        worker_strategy_versions="fixture_strategy@1",
        worker_instrument="EUR/USD",
        worker_timeframe="1h",
    )

    status = worker.run_once()

    assert status.eligible_strategy_count == 0
    assert "fixture_strategy@1:STRATEGY_NOT_ELIGIBLE" in status.last_reason_codes
    assert status.last_decision == "NO_TRADE"
    assert worker.dependencies.paper_engine.store.orders("missing") == ()


def test_incomplete_latest_bar_is_blocked_before_any_activity(tmp_path) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    market_store = InMemoryMarketDataStore()
    instrument = Instrument(
        canonical_symbol="EUR/USD",
        asset_class=AssetClass.FOREX,
        base_currency="EUR",
        quote_currency="USD",
    )
    latest_timestamp = now - timedelta(minutes=30)
    market_store.upsert_bars(
        [
            MarketBar(
                instrument=instrument,
                timeframe=Timeframe.H1,
                timestamp=latest_timestamp,
                open=Decimal("1.10"),
                high=Decimal("1.11"),
                low=Decimal("1.09"),
                close=Decimal("1.105"),
                source="local",
                ingestion_timestamp=now - timedelta(minutes=1),
            )
        ]
    )
    market_store.record_dataset(
        DatasetMetadata(
            dataset_version="qualified-incomplete",
            provider="local",
            symbol="EUR/USD",
            timeframe="1h",
            start=latest_timestamp,
            end=now,
            adjustment_policy=AdjustmentPolicy.RAW,
            configuration_version="test",
            quality_status="clean",
            created_at=now,
        )
    )
    worker = _worker(
        tmp_path,
        worker_instrument="EUR/USD",
        worker_timeframe="1h",
        worker_dataset_version="qualified-incomplete",
        worker_data_max_age_seconds=7200,
    )
    worker.dependencies = WorkerDependencies(
        market_store=market_store,
        paper_engine=worker.dependencies.paper_engine,
        governance=worker.dependencies.governance,
    )
    worker._clock = lambda: now

    status = worker.run_once()

    assert status.data_status == "BLOCKED"
    assert status.data_reason == "DATA_INCOMPLETE"
    assert status.last_decision == "NO_TRADE"
    assert status.paper_order_count == 0


def test_dataset_without_latest_bar_coverage_is_blocked(tmp_path) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    market_store = InMemoryMarketDataStore()
    instrument = Instrument(
        canonical_symbol="EUR/USD",
        asset_class=AssetClass.FOREX,
        base_currency="EUR",
        quote_currency="USD",
    )
    latest_timestamp = now - timedelta(hours=1)
    market_store.upsert_bars(
        [
            MarketBar(
                instrument=instrument,
                timeframe=Timeframe.H1,
                timestamp=latest_timestamp,
                open=Decimal("1.10"),
                high=Decimal("1.11"),
                low=Decimal("1.09"),
                close=Decimal("1.105"),
                source="local",
                ingestion_timestamp=now,
                bid=Decimal("1.104"),
                ask=Decimal("1.106"),
                quote_timestamp=now,
            )
        ]
    )
    market_store.record_dataset(
        DatasetMetadata(
                dataset_version="qualified-covered-too-early",
            provider="local",
            symbol="EUR/USD",
            timeframe="1h",
            start=latest_timestamp,
            end=now - timedelta(minutes=1),
            adjustment_policy=AdjustmentPolicy.RAW,
            configuration_version="test",
            quality_status="clean",
            created_at=now,
        )
    )
    worker = _worker(
        tmp_path,
        worker_instrument="EUR/USD",
        worker_timeframe="1h",
            worker_dataset_version="qualified-covered-too-early",
    )
    worker.dependencies = WorkerDependencies(
        market_store=market_store,
        paper_engine=worker.dependencies.paper_engine,
        governance=worker.dependencies.governance,
        system_health_provider=worker.dependencies.system_health_provider,
    )
    worker._clock = lambda: now

    status = worker.run_once()

    assert status.data_status == "BLOCKED"
    assert status.data_reason == "DATA_DATASET_COVERAGE"
    assert status.last_decision == "NO_TRADE"
    assert status.paper_order_count == 0


def test_fixture_dataset_metadata_is_blocked_with_local_source(tmp_path) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    instrument = Instrument(
        canonical_symbol="EUR/USD",
        asset_class=AssetClass.FOREX,
        base_currency="EUR",
        quote_currency="USD",
    )
    bar = MarketBar(
        instrument=instrument,
        timeframe=Timeframe.H1,
        timestamp=now - timedelta(hours=1),
        open=Decimal("1.10"),
        high=Decimal("1.11"),
        low=Decimal("1.09"),
        close=Decimal("1.105"),
        source="local",
        ingestion_timestamp=now,
        bid=Decimal("1.104"),
        ask=Decimal("1.106"),
        quote_timestamp=now,
    )
    market_store = InMemoryMarketDataStore()
    market_store.upsert_bars((bar,))
    market_store.record_dataset(
        DatasetMetadata(
            dataset_version="qualified-local-data",
            dataset_hash=market_bar_content_hash((bar,)),
            provider="local",
            symbol="EUR/USD",
            timeframe="1h",
            start=bar.timestamp,
            end=now,
            adjustment_policy=AdjustmentPolicy.RAW,
            configuration_version="synthetic-fixture-v1",
            quality_status="clean",
            created_at=now,
        )
    )
    worker = _worker(
        tmp_path,
        worker_instrument="EUR/USD",
        worker_timeframe="1h",
        worker_data_source="local",
        worker_dataset_version="qualified-local-data",
    )
    worker.dependencies = WorkerDependencies(
        market_store=market_store,
        paper_engine=worker.dependencies.paper_engine,
        governance=worker.dependencies.governance,
        pipeline=worker.dependencies.pipeline,
        system_health_provider=worker.dependencies.system_health_provider,
    )
    worker._clock = lambda: now

    status = worker.run_once()

    assert status.data_status == "BLOCKED"
    assert status.data_reason == "DATA_FIXTURE_NOT_OPERATIONAL"
    assert status.last_decision == "NO_TRADE"


@pytest.mark.parametrize(
    ("dataset_hash", "expected_reason"),
    ((None, "DATA_IDENTITY_UNAVAILABLE"), ("wrong-content", "DATA_IDENTITY_MISMATCH")),
)
def test_dataset_content_identity_is_verified_before_activity(
    tmp_path, dataset_hash: str | None, expected_reason: str
) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    instrument = Instrument(
        canonical_symbol="EUR/USD",
        asset_class=AssetClass.FOREX,
        base_currency="EUR",
        quote_currency="USD",
    )
    latest_timestamp = now - timedelta(hours=1)
    worker = _worker(
        tmp_path,
        worker_instrument="EUR/USD",
        worker_timeframe="1h",
        worker_data_source="local",
        worker_dataset_version="qualified-content-identity",
    )
    worker.dependencies.market_store.upsert_bars(
        [
            MarketBar(
                instrument=instrument,
                timeframe=Timeframe.H1,
                timestamp=latest_timestamp,
                open=Decimal("1.10"),
                high=Decimal("1.11"),
                low=Decimal("1.09"),
                close=Decimal("1.105"),
                source="local",
                ingestion_timestamp=now,
                bid=Decimal("1.104"),
                ask=Decimal("1.106"),
                quote_timestamp=now,
            )
        ]
    )
    worker.dependencies.market_store.record_dataset(
        DatasetMetadata(
                dataset_version="qualified-content-identity",
            dataset_hash=dataset_hash,
            provider="local",
            symbol="EUR/USD",
            timeframe="1h",
            start=latest_timestamp,
            end=now,
            adjustment_policy=AdjustmentPolicy.RAW,
            configuration_version="test",
            quality_status="clean",
            created_at=now,
        )
    )
    worker._clock = lambda: now

    status = worker.run_once()

    assert status.data_status == "BLOCKED"
    assert status.data_reason == expected_reason
    assert status.last_decision == "NO_TRADE"
    assert status.paper_order_count == 0


def test_worker_rejects_latest_bar_from_a_different_adjustment_policy(tmp_path) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    instrument = Instrument(
        canonical_symbol="EUR/USD",
        asset_class=AssetClass.FOREX,
        base_currency="EUR",
        quote_currency="USD",
    )
    raw_timestamp = now - timedelta(hours=2)
    adjusted_timestamp = now - timedelta(hours=1)
    raw_bar = MarketBar(
        instrument=instrument,
        timeframe=Timeframe.H1,
        timestamp=raw_timestamp,
        open=Decimal("1.10"),
        high=Decimal("1.11"),
        low=Decimal("1.09"),
        close=Decimal("1.105"),
        source="local",
        ingestion_timestamp=now,
        bid=Decimal("1.104"),
        ask=Decimal("1.106"),
        quote_timestamp=now,
    )
    adjusted_bar = raw_bar.model_copy(
        update={
            "adjustment_policy": AdjustmentPolicy.ADJUSTED,
            "timestamp": adjusted_timestamp,
        }
    )
    worker = _worker(
        tmp_path,
        worker_instrument="EUR/USD",
        worker_timeframe="1h",
        worker_data_source="local",
        worker_dataset_version="qualified-raw-data",
    )
    worker.dependencies.market_store.upsert_bars((raw_bar, adjusted_bar))
    raw_hash = market_bar_content_hash((raw_bar,))
    worker.dependencies.market_store.record_dataset(
        DatasetMetadata(
            dataset_version="qualified-raw-data",
            dataset_hash=raw_hash,
            provider="local",
            symbol="EUR/USD",
            timeframe="1h",
            start=raw_timestamp,
            end=now,
            adjustment_policy=AdjustmentPolicy.RAW,
            configuration_version="test",
            quality_status="clean",
            created_at=now,
        )
    )
    worker._clock = lambda: now

    status = worker.run_once()

    assert status.data_reason == "DATA_ADJUSTMENT_POLICY_MISMATCH"
    assert status.last_decision == "NO_TRADE"
    assert status.paper_order_count == 0


def test_reconciliation_failure_blocks_activity_and_is_visible(tmp_path, monkeypatch) -> None:
    worker = _worker(tmp_path, worker_account_id="paper-account")

    def fail_reconciliation(account_id: str) -> None:
        raise RuntimeError("synthetic reconciliation failure")

    monkeypatch.setattr(worker.dependencies.paper_engine, "reconcile", fail_reconciliation)

    status = worker.run_once()

    assert status.health.value == "FAILED"
    assert status.reconciliation_status == "FAILED"
    assert "RECONCILIATION_FAILED" in status.last_reason_codes
    assert "WORKER_CYCLE_FAILED" in status.last_reason_codes
    assert any(
        event.stage == "reconciliation" and event.status == "FAILED"
        for event in status.last_trace
    )
    assert status.last_decision == "NO_TRADE"


def test_explicit_reconciliation_failure_remains_failed(tmp_path, monkeypatch) -> None:
    worker = _worker(tmp_path, worker_account_id="paper-account")
    monkeypatch.setattr(worker, "_reconcile", lambda: ("FAILED", (), 0, 0))

    status = worker.run_once()

    assert status.health.value == "FAILED"
    assert status.reconciliation_status == "FAILED"
    assert "RECONCILIATION_FAILED" in status.last_reason_codes
    assert status.last_decision == "NO_TRADE"


def test_reconciliation_failure_overwrites_prior_healthy_status(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    worker = _worker(tmp_path, worker_account_id="paper-account")
    firewall = worker.dependencies.pipeline.risk_firewall  # type: ignore[union-attr]
    worker.dependencies.paper_engine.create_account(
        account_id="paper-account",
        account_currency="USD",
        starting_cash=Decimal("10000"),
        risk_capacity=Decimal("10000"),
        daily_loss_limit=firewall.parameters.daily_loss_limit,
        max_drawdown=firewall.parameters.max_drawdown,
        risk_policy_id=firewall.policy_id,
        risk_policy_configuration_id=firewall.configuration_id,
        timestamp=now,
    )
    worker._clock = lambda: now

    first = worker.run_once()

    assert first.reconciliation_status == "HEALTHY"

    def fail_reconciliation(account_id: str) -> None:
        raise RuntimeError("synthetic reconciliation failure after healthy state")

    monkeypatch.setattr(worker.dependencies.paper_engine, "reconcile", fail_reconciliation)

    second = worker.run_once()

    assert second.health.value == "FAILED"
    assert second.reconciliation_status == "FAILED"
    assert "RECONCILIATION_FAILED" in second.last_reason_codes
    assert "WORKER_CYCLE_FAILED" in second.last_reason_codes
    assert any(
        event.stage == "reconciliation" and event.status == "FAILED"
        for event in second.last_trace
    )


def test_persistence_health_failure_has_explicit_reason_code(tmp_path, monkeypatch) -> None:
    worker = _worker(tmp_path)

    def fail_health_check() -> None:
        raise RuntimeError("synthetic persistence health failure")

    monkeypatch.setattr(worker.store, "health_check", fail_health_check)

    status = worker.run_once()

    assert status.health.value == "FAILED"
    assert "PERSISTENCE_HEALTH_CHECK_FAILED" in status.last_reason_codes
    assert "WORKER_CYCLE_FAILED" in status.last_reason_codes
    assert status.data_reason == "DATA_NOT_EVALUATED"
    assert status.last_decision == "NO_TRADE"


def test_single_worker_exclusion_and_graceful_shutdown(tmp_path) -> None:
    worker = _worker(tmp_path)
    worker._begin(reset_stop=False)
    try:
        second = _worker(tmp_path)
        with pytest.raises(WorkerBusy):
            second.run_once()
    finally:
        worker._finish()

    stopping = _worker(tmp_path)
    stopping.request_shutdown()
    status = stopping.run_forever()
    assert status.worker_state.value == "STOPPED"


def test_same_account_different_workloads_are_excluded(tmp_path) -> None:
    first = _worker(
        tmp_path,
        worker_workload_id="workload-one",
        worker_account_id="paper-account",
    )
    first._begin(reset_stop=False)
    try:
        second = _worker(
            tmp_path,
            worker_workload_id="workload-two",
            worker_account_id="paper-account",
        )
        with pytest.raises(WorkerBusy):
            second.run_once()
    finally:
        first._finish()


def test_active_account_lease_rejects_same_owner_different_workload(tmp_path) -> None:
    started = datetime(2026, 1, 1, 12, tzinfo=UTC)
    store = _worker(tmp_path).store
    lease_key = "account:paper-account"

    assert store.acquire_lease(
        "workload-one",
        "shared-owner",
        now=started,
        duration=timedelta(seconds=10),
        lease_key=lease_key,
    )
    assert not store.acquire_lease(
        "workload-two",
        "shared-owner",
        now=started + timedelta(seconds=1),
        duration=timedelta(seconds=10),
        lease_key=lease_key,
    )
    assert store.lease_is_valid(
        "workload-one", "shared-owner", now=started, lease_key=lease_key
    )
    store.release_lease("workload-one", "shared-owner", lease_key=lease_key)


def test_reclaimed_lease_records_new_acquisition_time(tmp_path) -> None:
    started = datetime(2026, 1, 1, 12, tzinfo=UTC)
    reclaimed = started + timedelta(seconds=2)
    renewed = reclaimed + timedelta(seconds=1)
    store = _worker(tmp_path).store

    assert store.acquire_lease(
        "workload-one", "old-owner", now=started, duration=timedelta(seconds=1)
    )
    assert store.acquire_lease(
        "workload-two", "new-owner", now=reclaimed, duration=timedelta(seconds=10)
    )
    assert store.acquire_lease(
        "workload-two", "new-owner", now=renewed, duration=timedelta(seconds=10)
    )

    with store.engine.connect() as connection:
        acquired_at = connection.execute(
            select(worker_leases.c.acquired_at).where(
                worker_leases.c.workload_id == "workload-two"
            )
        ).scalar_one()
    assert _utc(acquired_at) == reclaimed
    store.release_lease("workload-two", "new-owner")


def test_account_scoped_status_rejects_stale_workload_claim(tmp_path) -> None:
    started = datetime(2026, 1, 1, 12, tzinfo=UTC)
    first = _worker(
        tmp_path,
        worker_workload_id="workload-one",
        worker_account_id="paper-account",
        worker_lease_seconds=1,
    )
    first._clock = lambda: started
    first._begin(reset_stop=True)
    try:
        replacement_time = started + timedelta(seconds=2)
        second = _worker(
            tmp_path,
            worker_workload_id="workload-two",
            worker_account_id="paper-account",
            worker_lease_seconds=10,
        )
        second._clock = lambda: replacement_time
        second._begin(reset_stop=True)
        try:
            observed = first.store.observed_status(
                "workload-one",
                now=replacement_time,
                lease_key=first._lease_key(),
            )
        finally:
            second._finish()
    finally:
        first._finish()

    assert observed.worker_state is WorkerState.UNKNOWN
    assert observed.health is WorkerHealth.FAILED
    assert observed.last_error == "WORKER_LEASE_OWNED_BY_OTHER_WORKLOAD"


def test_account_claim_fences_shared_owner_mutations(tmp_path) -> None:
    started = datetime(2026, 1, 1, 12, tzinfo=UTC)
    replacement_time = started + timedelta(seconds=2)
    store = _worker(tmp_path, worker_lease_seconds=1).store
    lease_key = "account:paper-account"
    assert store.acquire_lease(
        "workload-one",
        "shared-owner",
        now=started,
        duration=timedelta(seconds=1),
        lease_key=lease_key,
    )
    assert store.save_status_if_lease_owned(
        WorkerStatus.unknown("workload-one"),
        updated_at=started,
        owner_id="shared-owner",
        lease_key=lease_key,
    )
    assert store.acquire_lease(
        "workload-two",
        "shared-owner",
        now=replacement_time,
        duration=timedelta(seconds=10),
        lease_key=lease_key,
    )

    assert not store.save_status_if_lease_owned(
        WorkerStatus.unknown("workload-one"),
        updated_at=replacement_time,
        owner_id="shared-owner",
        lease_key=lease_key,
    )
    assert not store.heartbeat(
        "workload-one",
        "shared-owner",
        now=replacement_time,
        duration=timedelta(seconds=10),
        lease_key=lease_key,
    )
    outcome = CycleOutcome(
        cycle_id="cycle:stale-claim",
        decision_key="decision:stale-claim",
        action="NO_TRADE",
        health=WorkerHealth.BLOCKED,
        data_status="BLOCKED",
        data_reason="DATA_UNAVAILABLE",
        eligible_strategy_count=0,
        reconciliation_status="UNKNOWN",
        reason_codes=("DATA_UNAVAILABLE",),
    )
    assert (
        store.record_cycle_if_lease_owned(
            workload_id="workload-one",
            decision_key=outcome.decision_key,
            started_at=started,
            completed_at=replacement_time,
            outcome=outcome,
            owner_id="shared-owner",
            lease_key=lease_key,
        )
        is None
    )
    store.release_lease("workload-one", "shared-owner", lease_key=lease_key)
    assert store.lease_is_valid(
        "workload-two", "shared-owner", now=replacement_time, lease_key=lease_key
    )
    store.release_lease("workload-two", "shared-owner", lease_key=lease_key)


def test_expired_worker_cannot_clobber_replacement_owner_status(tmp_path) -> None:
    started = datetime(2026, 1, 1, 12, tzinfo=UTC)
    worker = _worker(tmp_path, worker_lease_seconds=1)
    worker._clock = lambda: started
    worker._begin(reset_stop=True)

    replacement_time = started + timedelta(seconds=2)
    assert worker.store.acquire_lease(
        "default",
        "replacement-owner",
        now=replacement_time,
        duration=timedelta(seconds=10),
    )
    worker._clock = lambda: replacement_time

    worker._finish()

    status = worker.store.status("default")
    assert status.worker_state.value == "STARTING"
    assert worker.store.lease_is_valid(
        "default", "replacement-owner", now=replacement_time
    )
    worker.store.release_lease("default", "replacement-owner")


def test_worker_store_rejects_non_utc_lease_and_cycle_timestamps(tmp_path) -> None:
    store = _worker(tmp_path).store
    aware = datetime(2026, 1, 1, 12, tzinfo=UTC)
    naive = datetime(2026, 1, 1, 12)

    with pytest.raises(TimestampNormalizationError):
        store.acquire_lease(
            "default",
            "owner",
            now=naive,
            duration=timedelta(seconds=30),
        )

    assert store.acquire_lease(
        "default",
        "owner",
        now=aware,
        duration=timedelta(seconds=30),
    )
    with pytest.raises(TimestampNormalizationError):
        store.heartbeat(
            "default",
            "owner",
            now=naive,
            duration=timedelta(seconds=30),
        )

    outcome = CycleOutcome(
        cycle_id="cycle:utc-boundary",
        decision_key="decision:utc-boundary",
        action="NO_TRADE",
        health=WorkerHealth.BLOCKED,
        data_status="BLOCKED",
        data_reason="DATA_UNAVAILABLE",
        eligible_strategy_count=0,
        reconciliation_status="UNKNOWN",
        reason_codes=("DATA_UNAVAILABLE",),
    )
    with pytest.raises(TimestampNormalizationError):
        store.record_cycle(
            workload_id="default",
            decision_key=outcome.decision_key,
            started_at=naive,
            completed_at=aware,
            outcome=outcome,
        )

    with pytest.raises(ValueError, match="completion cannot precede"):
        store.record_cycle(
            workload_id="default",
            decision_key="decision:reversed-time",
            started_at=aware,
            completed_at=aware - timedelta(seconds=1),
            outcome=outcome,
        )

    with pytest.raises(TimestampNormalizationError):
        store.save_status(WorkerStatus.unknown("default"), updated_at=naive)


def test_worker_store_normalizes_non_utc_persisted_timestamps_on_read() -> None:
    persisted = datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=2)))

    normalized = _utc(persisted)

    assert normalized == datetime(2026, 1, 1, 10, tzinfo=UTC)
    assert normalized is not None
    assert normalized.tzinfo is UTC


def test_stale_owner_cycle_append_is_rejected_after_lease_replacement(tmp_path) -> None:
    started = datetime(2026, 1, 1, 12, tzinfo=UTC)
    replacement_time = started + timedelta(seconds=2)
    store = _worker(tmp_path, worker_lease_seconds=1).store
    assert store.acquire_lease(
        "default",
        "old-owner",
        now=started,
        duration=timedelta(seconds=1),
    )
    assert store.acquire_lease(
        "default",
        "replacement-owner",
        now=replacement_time,
        duration=timedelta(seconds=10),
    )
    outcome = CycleOutcome(
        cycle_id="cycle:stale-owner",
        decision_key="decision:stale-owner",
        action="NO_TRADE",
        health=WorkerHealth.BLOCKED,
        data_status="BLOCKED",
        data_reason="DATA_UNAVAILABLE",
        eligible_strategy_count=0,
        reconciliation_status="UNKNOWN",
        reason_codes=("DATA_UNAVAILABLE",),
    )

    persisted = store.record_cycle_if_lease_owned(
        workload_id="default",
        decision_key=outcome.decision_key,
        started_at=started,
        completed_at=replacement_time,
        outcome=outcome,
        owner_id="old-owner",
        lease_key="default",
    )

    assert persisted is None
    assert store.cycle_count("default") == 0
    store.release_lease("default", "replacement-owner")


def test_expired_owner_cannot_resurrect_lease_with_heartbeat(tmp_path) -> None:
    started = datetime(2026, 1, 1, 12, tzinfo=UTC)
    expired = started + timedelta(seconds=2)
    store = _worker(tmp_path, worker_lease_seconds=1).store

    assert store.acquire_lease(
        "default", "old-owner", now=started, duration=timedelta(seconds=1)
    )
    assert not store.heartbeat(
        "default",
        "old-owner",
        now=expired,
        duration=timedelta(seconds=10),
    )
    assert store.acquire_lease(
        "default", "replacement-owner", now=expired, duration=timedelta(seconds=10)
    )
    store.release_lease("default", "replacement-owner")


def test_lease_validity_rejects_replaced_account_workload(tmp_path) -> None:
    started = datetime(2026, 1, 1, 12, tzinfo=UTC)
    replacement_time = started + timedelta(seconds=2)
    store = _worker(tmp_path, worker_lease_seconds=1).store
    lease_key = "account:paper-account"

    assert store.acquire_lease(
        "workload-one",
        "old-owner",
        now=started,
        duration=timedelta(seconds=1),
        lease_key=lease_key,
    )
    assert store.acquire_lease(
        "workload-two",
        "replacement-owner",
        now=replacement_time,
        duration=timedelta(seconds=10),
        lease_key=lease_key,
    )
    assert not store.lease_is_valid(
        "workload-one", None, now=replacement_time, lease_key=lease_key
    )
    assert store.lease_is_valid(
        "workload-two", "replacement-owner", now=replacement_time, lease_key=lease_key
    )
    store.release_lease("workload-two", "replacement-owner", lease_key=lease_key)


def test_lease_loss_before_execution_is_fail_closed(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    worker = _worker(
        tmp_path,
        worker_account_id="paper-account",
        worker_instrument="EUR/USD",
        worker_timeframe="1h",
        worker_data_source="local",
        worker_dataset_version="qualified-actionable",
        worker_strategy_versions="fixture_strategy@1",
    )
    firewall = worker.dependencies.pipeline.risk_firewall  # type: ignore[union-attr]
    worker.dependencies.paper_engine.create_account(
        account_id="paper-account",
        account_currency="USD",
        starting_cash=Decimal("10000"),
        risk_capacity=Decimal("10000"),
        daily_loss_limit=firewall.parameters.daily_loss_limit,
        max_drawdown=firewall.parameters.max_drawdown,
        risk_policy_id=firewall.policy_id,
        risk_policy_configuration_id=firewall.configuration_id,
        timestamp=now,
    )
    worker.dependencies.market_store.upsert_bars(
        (
            MarketBar(
                instrument=Instrument(
                    canonical_symbol="EUR/USD",
                    asset_class=AssetClass.FOREX,
                    base_currency="EUR",
                    quote_currency="USD",
                ),
                timeframe=Timeframe.H1,
                timestamp=now - timedelta(hours=1),
                open=Decimal("1.10"),
                high=Decimal("1.11"),
                low=Decimal("1.09"),
                close=Decimal("1.105"),
                source="local",
                ingestion_timestamp=now,
                bid=Decimal("1.104"),
                ask=Decimal("1.106"),
                quote_timestamp=now,
            ),
        )
    )
    monkeypatch.setattr(
        worker,
        "_admit_data",
        lambda _: DataAdmission(
            "HEALTHY", None, now - timedelta(hours=1), "qualified-actionable", "fixture-hash"
        ),
    )
    monkeypatch.setattr(worker, "_resolve_strategy_eligibility", lambda: (1, ()))
    pipeline_calls = 0

    class SpyPipeline:
        def run(self, **_: object) -> PaperPipelineOutcome:
            nonlocal pipeline_calls
            pipeline_calls += 1
            return PaperPipelineOutcome(
                action="ORDER_SUBMITTED",
                health="HEALTHY",
                reason_codes=("UNEXPECTED_PIPELINE_ENTRY",),
                trace=(),
            )

    worker.dependencies = WorkerDependencies(
        market_store=worker.dependencies.market_store,
        paper_engine=worker.dependencies.paper_engine,
        governance=worker.dependencies.governance,
        pipeline=cast("PaperDecisionPipeline", SpyPipeline()),
        system_health_provider=worker.dependencies.system_health_provider,
    )
    worker._clock = lambda: now

    def lose_lease(
        workload_id: str,
        owner_id: str,
        *,
        now: datetime,
        duration: timedelta,
        lease_key: str | None = None,
    ) -> bool:
        worker.store.release_lease(workload_id, owner_id, lease_key=lease_key)
        return False

    monkeypatch.setattr(worker.store, "heartbeat", lose_lease)

    status = worker.run_once()

    assert status.worker_state.value == "UNKNOWN"
    assert status.health.value == "FAILED"
    assert status.last_error == "WORKER_LEASE_EXPIRED"
    assert pipeline_calls == 0
    assert worker.store.cycle_count("default") == 0
    assert worker.dependencies.paper_engine.store.orders("paper-account") == ()


def test_lease_loss_after_pipeline_stops_worker_before_next_cycle(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    worker = _worker(
        tmp_path,
        worker_account_id="paper-account",
        worker_instrument="EUR/USD",
        worker_timeframe="1h",
        worker_data_source="local",
        worker_dataset_version="qualified-actionable",
        worker_strategy_versions="fixture_strategy@1",
    )
    latest = SimpleNamespace(
        symbol="EUR/USD",
        timeframe=Timeframe.H1,
        timestamp=now - timedelta(hours=1),
        bid=Decimal("1.104"),
        ask=Decimal("1.106"),
        quote_timestamp=now,
    )
    monkeypatch.setattr(worker, "_reconcile", lambda: ("HEALTHY", (), 0, 0))
    monkeypatch.setattr(
        worker,
        "_admit_data",
        lambda _: DataAdmission("HEALTHY", None, latest.timestamp, "fixture", "hash"),
    )
    monkeypatch.setattr(worker, "_resolve_strategy_eligibility", lambda: (1, ()))
    monkeypatch.setattr(
        worker.dependencies.market_store, "latest_bar", lambda *args, **kwargs: latest
    )
    monkeypatch.setattr(
        worker.dependencies.market_store, "query_bars", lambda *args, **kwargs: (latest,)
    )
    monkeypatch.setattr(worker.dependencies.market_store, "quality_events", lambda: ())

    class LeaseLosingPipeline:
        calls = 0

        def run(self, **_: object) -> PaperPipelineOutcome:
            self.calls += 1
            worker.store.release_lease(
                worker.settings.worker_workload_id,
                worker.owner_id,
                lease_key=worker._lease_key(),
            )
            return PaperPipelineOutcome(
                action="NO_TRADE",
                health="HEALTHY",
                reason_codes=("FIXTURE_NO_TRADE",),
                trace=(),
            )

    pipeline = LeaseLosingPipeline()
    worker.dependencies = WorkerDependencies(
        market_store=worker.dependencies.market_store,
        paper_engine=worker.dependencies.paper_engine,
        governance=worker.dependencies.governance,
        pipeline=cast("PaperDecisionPipeline", pipeline),
        system_health_provider=worker.dependencies.system_health_provider,
    )
    worker._clock = lambda: now

    status = worker.run_once()

    assert pipeline.calls == 1
    assert status.worker_state.value == "UNKNOWN"
    assert status.health.value == "FAILED"
    assert status.last_error == "WORKER_LEASE_EXPIRED"
    assert worker._stop_event.is_set()
    assert worker.store.cycle_count("default") == 0
    assert worker.dependencies.paper_engine.store.orders("paper-account") == ()


def test_status_surfaces_expired_worker_lease_as_failed_unknown(tmp_path) -> None:
    worker = _worker(tmp_path, worker_lease_seconds=1)
    started = datetime(2026, 1, 1, 12, tzinfo=UTC)
    worker._clock = lambda: started
    worker._begin(reset_stop=False)
    try:
        current = worker.store.status("default")
        observed = worker.store.observed_status(
            "default",
            now=started + timedelta(seconds=2),
        )
    finally:
        worker._finish()

    assert current.worker_state.value == "STARTING"
    assert observed.worker_state.value == "UNKNOWN"
    assert observed.health.value == "FAILED"
    assert observed.last_error == "WORKER_LEASE_EXPIRED"


def test_supervisor_failure_persists_failed_status_before_stop(tmp_path, monkeypatch) -> None:
    worker = _worker(tmp_path)

    def fail_wait() -> None:
        raise WorkerError("lease heartbeat failed")

    monkeypatch.setattr(worker, "_wait_for_next_cycle", fail_wait)

    with pytest.raises(WorkerError, match="lease heartbeat failed"):
        worker.run_forever()

    status = worker.store.status("default")
    assert status.worker_state.value == "STOPPED"
    assert status.health.value == "FAILED"
    assert status.last_error == "lease heartbeat failed"


def test_live_mode_is_rejected_even_when_both_live_switches_are_set(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        trading_mode=TradingMode.LIVE,
        live_trading_enabled=True,
        database_url=f"sqlite:///{tmp_path / 'live.db'}",
    )
    engine = create_database_engine(settings.database_url)
    store = WorkerStore(engine)
    store.create_schema()
    with pytest.raises(WorkerConfigurationError, match="PAPER-only"):
        PaperWorker(
            settings,
            store=store,
            dependencies=WorkerDependencies(
                market_store=cast("MarketDataStore", SimpleNamespace()),
                paper_engine=cast("PaperTradingEngine", SimpleNamespace()),
                governance=None,
            ),
        )
