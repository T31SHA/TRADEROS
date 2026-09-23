"""SOFTWARE DEMONSTRATION / SYNTHETIC FIXTURE for the PAPER pipeline.

This test is isolated application coverage. It is not empirical qualification,
does not register evidence, and does not create a production approval.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import traderos.strategies.runtime as runtime_identity_module
from traderos.allocation import AllocationConflictPolicy, AllocationPolicy, BudgetLimit
from traderos.core.config import Settings, TradingMode
from traderos.data.bars import MarketBar
from traderos.data.hashing import market_bar_content_hash
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.lineage import AdjustmentPolicy, DatasetMetadata
from traderos.data.quality import DataQualityEvent, QualityCode, QualitySeverity
from traderos.data.timeframes import Timeframe
from traderos.database.connection import create_database_engine
from traderos.database.store import SqlAlchemyMarketDataStore
from traderos.paper import PaperTradingEngine, SqlAlchemyPaperStore
from traderos.regimes import EmaPercentileRegimeDetector
from traderos.regimes.errors import RegimeAnalysisError
from traderos.research.strategy_lifecycle import (
    FeatureDefinition,
    RegisteredImplementation,
    StrategyArtifact,
    StrategyParameter,
)
from traderos.research.strategy_service import StrategyGovernanceService
from traderos.risk import RiskFirewall, SystemHealthSnapshot, SystemHealthStatus
from traderos.signals import MajorityVoteFusionPolicy
from traderos.signals.errors import SignalFusionError
from traderos.strategies import ForexTrendFollowingStrategy
from traderos.strategies.errors import StrategyCausalityError
from traderos.strategies.runtime import runtime_content_identity
from traderos.worker import PaperWorker, WorkerDependencies
from traderos.worker.pipeline import PaperDecisionPipeline, StrategyRuntimeBinding
from traderos.worker.store import WorkerStore

ACCOUNT_ID = "synthetic-paper-pipeline"
INSTRUMENT = Instrument(
    canonical_symbol="EUR/USD",
    asset_class=AssetClass.FOREX,
    base_currency="EUR",
    quote_currency="USD",
    trading_currency="USD",
)


def _bars() -> tuple[MarketBar, ...]:
    latest = datetime(2026, 1, 5, 12, tzinfo=UTC)
    start = latest - timedelta(hours=69)
    result: list[MarketBar] = []
    for index in range(70):
        timestamp = start + timedelta(hours=index)
        close = Decimal("1.1000") + Decimal(index) * Decimal("0.0001")
        quote_timestamp = timestamp + timedelta(hours=1)
        result.append(
            MarketBar(
                instrument=INSTRUMENT,
                timeframe=Timeframe.H1,
                timestamp=timestamp,
                open=close - Decimal("0.00005"),
                high=close + Decimal("0.00010"),
                low=close - Decimal("0.00010"),
                close=close,
                source="synthetic-fixture",
                ingestion_timestamp=quote_timestamp,
                bid=close - Decimal("0.00005"),
                ask=close + Decimal("0.00005"),
                quote_timestamp=quote_timestamp,
            )
        )
    return tuple(result)


def _artifact(strategy: ForexTrendFollowingStrategy) -> StrategyArtifact:
    return StrategyArtifact(
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.strategy_version,
        family_id="synthetic-baseline",
        author="software-test",
        implementation=RegisteredImplementation("forex_trend_following", "1"),
        instruments=(INSTRUMENT.canonical_symbol,),
        timeframes=(Timeframe.H1.value,),
        parameters=tuple(
            StrategyParameter(name, value) for name, value in strategy.parameters.items()
        ),
        features=(
            FeatureDefinition("ema", "1", (StrategyParameter("window", 10),)),
            FeatureDefinition("ema", "1", (StrategyParameter("window", 30),)),
        ),
        training_reference=None,
        validation_reference=None,
        oos_reference=None,
        forecast_semantics="direction-only synthetic fixture",
        holding_period=None,
        turnover_assumption=None,
        cost_model_reference=None,
        capacity_model_reference=None,
        regime_dependencies=("ema_percentile_regime.v1",),
        risk_dependencies=("risk_firewall.v1",),
        known_failure_modes=("synthetic fixture",),
        code_identity=runtime_content_identity(strategy),
        dataset_identity="synthetic-fixture-dataset",
        configuration_identity=None,
        environment_reference="pytest",
    )


def _pipeline(
    tmp_path: Path, firewall: RiskFirewall | None = None
) -> tuple[PaperDecisionPipeline, PaperTradingEngine]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'paper.db'}")
    store = SqlAlchemyPaperStore(engine)
    store.create_schema_for_testing()
    paper = PaperTradingEngine(store)
    firewall = firewall or RiskFirewall()
    timestamp = _bars()[-1].quote_timestamp
    assert timestamp is not None
    paper.create_account(
        account_id=ACCOUNT_ID,
        account_currency="USD",
        starting_cash=Decimal("100000"),
        risk_capacity=Decimal("100000"),
        daily_loss_limit=firewall.parameters.daily_loss_limit,
        max_drawdown=firewall.parameters.max_drawdown,
        risk_policy_id=firewall.policy_id,
        risk_policy_configuration_id=firewall.configuration_id,
        timestamp=timestamp,
    )
    strategy = ForexTrendFollowingStrategy()
    allocation_policy = AllocationPolicy(
        policy_id="synthetic-allocation",
        policy_version="1",
        max_gross_exposure=Decimal("100000"),
        max_net_exposure=Decimal("100000"),
        max_incremental_exposure=Decimal("100000"),
        max_turnover_per_decision=Decimal("100000"),
        instrument_limits=(BudgetLimit(INSTRUMENT.canonical_symbol, Decimal("100000")),),
        strategy_budgets=(BudgetLimit("forex_trend_following@1", Decimal("100000")),),
        family_budgets=(BudgetLimit("synthetic-baseline", Decimal("100000")),),
        asset_class_limits=(),
        conflict_policy=AllocationConflictPolicy.NET_OPPOSING,
        allocation_increment=Decimal("0.000001"),
    )
    governance = SimpleNamespace(
        explain_strategy_eligibility=lambda strategy_id, version: SimpleNamespace(
            operationally_eligible=True, reasons=()
        ),
        get_strategy_artifact=lambda strategy_id, version: _artifact(strategy),
    )
    pipeline = PaperDecisionPipeline(
        paper_engine=paper,
        governance=cast(StrategyGovernanceService, governance),
        runtime_bindings=(
            StrategyRuntimeBinding(
                implementation_id="forex_trend_following",
                implementation_version="1",
                code_identity=runtime_content_identity(strategy),
                strategy=strategy,
            ),
        ),
        regime_detector=EmaPercentileRegimeDetector(),
        fusion_policy=MajorityVoteFusionPolicy(),
        risk_firewall=firewall,
        allocation_policy=allocation_policy,
    )
    return pipeline, paper


def test_worker_fixture_is_blocked_before_paper_order(tmp_path: Path) -> None:
    """SOFTWARE DEMONSTRATION / SYNTHETIC FIXTURE; not operational data.

    The governance double is isolated to this test and is not a production
    strategy approval or empirical qualification.
    """

    bars = _bars()
    engine = create_database_engine(f"sqlite:///{tmp_path / 'worker.db'}")
    paper_store = SqlAlchemyPaperStore(engine)
    paper_store.create_schema_for_testing()
    paper = PaperTradingEngine(paper_store)
    firewall = RiskFirewall()
    quote_timestamp = bars[-1].quote_timestamp
    assert quote_timestamp is not None
    paper.create_account(
        account_id=ACCOUNT_ID,
        account_currency="USD",
        starting_cash=Decimal("100000"),
        risk_capacity=Decimal("100000"),
        daily_loss_limit=firewall.parameters.daily_loss_limit,
        max_drawdown=firewall.parameters.max_drawdown,
        risk_policy_id=firewall.policy_id,
        risk_policy_configuration_id=firewall.configuration_id,
        timestamp=quote_timestamp,
    )

    market_store = SqlAlchemyMarketDataStore(engine)
    market_store.create_schema()
    market_store.upsert_bars(bars)
    stored_bars = market_store.query_bars(
        INSTRUMENT.canonical_symbol,
        Timeframe.H1,
        bars[0].timestamp,
        bars[-1].timestamp + Timeframe.H1.duration,
        source="synthetic-fixture",
        adjustment_policy=AdjustmentPolicy.RAW,
    )
    dataset_hash = market_bar_content_hash(stored_bars)
    market_store.record_dataset(
        DatasetMetadata(
            dataset_version="synthetic-worker-dataset",
            dataset_hash=dataset_hash,
            provider="synthetic-fixture",
            symbol=INSTRUMENT.canonical_symbol,
            timeframe=Timeframe.H1.value,
            start=bars[0].timestamp,
            end=bars[-1].timestamp + Timeframe.H1.duration,
            adjustment_policy=AdjustmentPolicy.RAW,
            configuration_version="software-demonstration",
            quality_status="clean",
            created_at=quote_timestamp,
        )
    )

    strategy = ForexTrendFollowingStrategy()
    artifact = replace(_artifact(strategy), dataset_identity=dataset_hash)
    governance = SimpleNamespace(
        explain_strategy_eligibility=lambda strategy_id, version: SimpleNamespace(
            operationally_eligible=True, reasons=()
        ),
        get_strategy_artifact=lambda strategy_id, version: artifact,
    )
    pipeline = PaperDecisionPipeline(
        paper_engine=paper,
        governance=cast(StrategyGovernanceService, governance),
        runtime_bindings=(
            StrategyRuntimeBinding(
                implementation_id="forex_trend_following",
                implementation_version="1",
                code_identity=runtime_content_identity(strategy),
                strategy=strategy,
            ),
        ),
        regime_detector=EmaPercentileRegimeDetector(),
        fusion_policy=MajorityVoteFusionPolicy(),
        risk_firewall=firewall,
    )
    settings = Settings(
        _env_file=None,
        trading_mode=TradingMode.PAPER,
        database_url=f"sqlite:///{tmp_path / 'worker.db'}",
        worker_account_id=ACCOUNT_ID,
        worker_instrument=INSTRUMENT.canonical_symbol,
        worker_timeframe=Timeframe.H1.value,
        worker_data_source="synthetic-fixture",
        worker_dataset_version="synthetic-worker-dataset",
        worker_strategy_versions="forex_trend_following@1",
    )
    worker = PaperWorker(
        settings,
        store=WorkerStore(engine),
        dependencies=WorkerDependencies(
            market_store=market_store,
            paper_engine=paper,
            governance=cast(StrategyGovernanceService, governance),
            pipeline=pipeline,
            system_health_provider=lambda timestamp, checks: SystemHealthSnapshot(
                timestamp, SystemHealthStatus.HEALTHY
            ),
        ),
        clock=lambda: quote_timestamp,
    )

    first = worker.run_once()
    second = worker.run_once()

    assert first.last_decision == "NO_TRADE"
    assert second.last_decision == "NO_TRADE"
    assert first.data_reason == "DATA_FIXTURE_NOT_OPERATIONAL"
    assert second.data_reason == "DATA_FIXTURE_NOT_OPERATIONAL"
    assert len(paper.store.orders(ACCOUNT_ID)) == 0
    assert paper.store.fills(ACCOUNT_ID) == ()


def test_governed_fixture_reaches_firewall_and_paper_engine_once(tmp_path: Path) -> None:
    bars = _bars()
    pipeline, paper = _pipeline(tmp_path)
    now = bars[-1].timestamp + bars[-1].timeframe.duration

    first = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=bars,
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=now,
        system_health=SystemHealthSnapshot(now, SystemHealthStatus.HEALTHY),
    )
    second = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=bars,
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=now,
        system_health=SystemHealthSnapshot(now, SystemHealthStatus.HEALTHY),
    )

    assert first.action == "ORDER_SUBMITTED"
    assert first.order is not None
    assert second.action == "NO_TRADE"
    assert "RISK_DUPLICATE_INTENT" in second.reason_codes
    assert len(paper.store.orders(ACCOUNT_ID)) == 1
    assert paper.store.fills(ACCOUNT_ID) == ()
    assert any(event.stage == "risk_firewall" for event in first.trace)
    assert any(event.stage == "paper_submission" for event in first.trace)
    risk_event = next(event for event in first.trace if event.stage == "risk_firewall")
    risk_metadata = dict(risk_event.metadata)
    assert risk_metadata["decision_id"] == first.order.risk_decision_id
    assert risk_metadata["status"] == "approve"
    assert json.loads(risk_metadata["authorization"])["action"] == "new_or_increase"
    assert any(
        item["check_id"] == "market_data_status"
        for item in json.loads(risk_metadata["checks"])
    )
    strategy_events = [event for event in first.trace if event.stage == "strategy_decision"]
    assert len(strategy_events) == 1
    assert "forex_trend_following@1:direction=long" in (strategy_events[0].reason or "")
    assert dict(strategy_events[0].metadata) == {
        "strategy_id": "forex_trend_following",
        "strategy_version": "1",
        "direction": "long",
        "intent_count": "1",
    }


def test_missing_approved_allocation_policy_blocks_new_order(tmp_path: Path) -> None:
    pipeline, paper = _pipeline(tmp_path)
    pipeline.allocation_policy = None
    bars = _bars()
    now = bars[-1].timestamp + bars[-1].timeframe.duration

    result = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=bars,
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=now,
        system_health=SystemHealthSnapshot(now, SystemHealthStatus.HEALTHY),
    )

    assert result.action == "NO_TRADE"
    assert "ALLOCATION_POLICY_UNAVAILABLE" in result.reason_codes
    assert paper.store.orders(ACCOUNT_ID) == ()


def test_strategy_disablement_between_allocation_and_execution_blocks_order(
    tmp_path: Path,
) -> None:
    pipeline, paper = _pipeline(tmp_path)
    calls = 0

    def eligibility(strategy_id: str, version: str) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        return SimpleNamespace(
            operationally_eligible=calls == 1,
            reasons=() if calls == 1 else ("STRATEGY_DISABLED",),
        )

    pipeline.governance.explain_strategy_eligibility = eligibility  # type: ignore[attr-defined]
    bars = _bars()
    now = bars[-1].timestamp + bars[-1].timeframe.duration

    result = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=bars,
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=now,
        system_health=SystemHealthSnapshot(now, SystemHealthStatus.HEALTHY),
    )

    assert result.action == "NO_TRADE"
    assert result.reason_codes == ("ALLOCATION_STRATEGY_GOVERNANCE_CHANGED",)
    assert paper.store.orders(ACCOUNT_ID) == ()


def test_stale_allocation_portfolio_revision_cannot_execute(tmp_path: Path) -> None:
    pipeline, paper = _pipeline(tmp_path)
    original_snapshot = paper.portfolio_risk_snapshot(ACCOUNT_ID)
    calls = 0

    def changing_snapshot(account_id: str):
        nonlocal calls
        calls += 1
        return original_snapshot if calls == 1 else replace(original_snapshot, revision=99)

    pipeline.paper_engine.portfolio_risk_snapshot = changing_snapshot  # type: ignore[method-assign]
    bars = _bars()
    now = bars[-1].timestamp + bars[-1].timeframe.duration

    result = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=bars,
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=now,
        system_health=SystemHealthSnapshot(now, SystemHealthStatus.HEALTHY),
    )

    assert result.action == "NO_TRADE"
    assert result.reason_codes == ("ALLOCATION_PORTFOLIO_REVISION_STALE",)
    assert paper.store.orders(ACCOUNT_ID) == ()


def test_unrelated_quality_event_is_not_entered_into_regime_context(tmp_path: Path) -> None:
    bars = _bars()
    pipeline, paper = _pipeline(tmp_path)
    now = bars[-1].timestamp + bars[-1].timeframe.duration
    unrelated = DataQualityEvent(
        occurred_at=now,
        severity=QualitySeverity.ERROR,
        code=QualityCode.INVALID_PRICE,
        message="unrelated provider error",
        source="other-provider",
        symbol="GBP/USD",
        timeframe="1h",
        bar_timestamp=bars[-1].timestamp,
    )

    result = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=bars,
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=now,
        system_health=SystemHealthSnapshot(now, SystemHealthStatus.HEALTHY),
        quality_events=(unrelated,),
    )

    assert result.action == "ORDER_SUBMITTED"
    assert result.health == "HEALTHY"
    assert len(paper.store.orders(ACCOUNT_ID)) == 1


def test_typed_strategy_failure_is_traced_and_cannot_submit(tmp_path: Path, monkeypatch) -> None:
    bars = _bars()
    pipeline, paper = _pipeline(tmp_path)
    strategy = pipeline._bindings[("forex_trend_following", "1")].strategy  # type: ignore[attr-defined]

    def fail(_: object) -> object:
        raise StrategyCausalityError("synthetic strategy failure")

    monkeypatch.setattr(strategy, "on_bar", fail)
    now = bars[-1].timestamp + bars[-1].timeframe.duration

    result = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=bars,
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=now,
        system_health=SystemHealthSnapshot(now, SystemHealthStatus.HEALTHY),
    )

    assert result.action == "NO_TRADE"
    assert result.health == "FAILED"
    assert result.reason_codes == ("STRATEGY_EVALUATION_FAILED",)
    assert any(
        event.stage == "strategy_evaluation"
        and event.status == "FAILED"
        and event.reason == "synthetic strategy failure"
        for event in result.trace
    )
    assert paper.store.orders(ACCOUNT_ID) == ()


def test_typed_regime_failure_is_traced_and_cannot_submit(tmp_path: Path, monkeypatch) -> None:
    bars = _bars()
    pipeline, paper = _pipeline(tmp_path)

    def fail(_: object) -> object:
        raise RegimeAnalysisError("synthetic regime failure")

    monkeypatch.setattr(pipeline.regime_detector, "evaluate", fail)
    now = bars[-1].timestamp + bars[-1].timeframe.duration

    result = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=bars,
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=now,
        system_health=SystemHealthSnapshot(now, SystemHealthStatus.HEALTHY),
    )

    assert result.action == "NO_TRADE"
    assert result.health == "FAILED"
    assert result.reason_codes == ("REGIME_EVALUATION_FAILED",)
    assert any(
        event.stage == "regime"
        and event.status == "FAILED"
        and event.reason == "synthetic regime failure"
        for event in result.trace
    )
    assert paper.store.orders(ACCOUNT_ID) == ()


def test_typed_fusion_failure_is_traced_and_cannot_submit(tmp_path: Path, monkeypatch) -> None:
    bars = _bars()
    pipeline, paper = _pipeline(tmp_path)

    def fail(_: object) -> object:
        raise SignalFusionError("synthetic fusion failure")

    monkeypatch.setattr(pipeline.fusion, "fuse", fail)
    now = bars[-1].timestamp + bars[-1].timeframe.duration

    result = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=bars,
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=now,
        system_health=SystemHealthSnapshot(now, SystemHealthStatus.HEALTHY),
    )

    assert result.action == "NO_TRADE"
    assert result.health == "FAILED"
    assert result.reason_codes == ("FUSION_FAILED",)
    assert any(
        event.stage == "fusion"
        and event.status == "FAILED"
        and event.reason == "synthetic fusion failure"
        for event in result.trace
    )
    assert paper.store.orders(ACCOUNT_ID) == ()


def test_pipeline_rejects_duplicate_strategy_configuration(tmp_path: Path) -> None:
    bars = _bars()
    pipeline, paper = _pipeline(tmp_path)
    now = bars[-1].timestamp + bars[-1].timeframe.duration

    result = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(
            ("forex_trend_following", "1"),
            ("forex_trend_following", "1"),
        ),
        bars=bars,
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=now,
        system_health=SystemHealthSnapshot(now, SystemHealthStatus.HEALTHY),
    )

    assert result.action == "NO_TRADE"
    assert result.health == "BLOCKED"
    assert result.reason_codes == ("STRATEGY_CONFIGURATION_DUPLICATE",)
    assert result.trace[-1].stage == "strategy_resolution"
    assert paper.store.orders(ACCOUNT_ID) == ()


def test_pipeline_rejects_duplicate_bar_timestamp(tmp_path: Path) -> None:
    bars = _bars()
    pipeline, paper = _pipeline(tmp_path)
    now = bars[-1].timestamp + bars[-1].timeframe.duration

    result = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=(*bars, bars[-1]),
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=now,
        system_health=SystemHealthSnapshot(now, SystemHealthStatus.HEALTHY),
    )

    assert result.action == "NO_TRADE"
    assert result.health == "BLOCKED"
    assert result.reason_codes == ("DATA_DUPLICATE_TIMESTAMP",)
    assert len(result.trace) == 1
    assert result.trace[0].stage == "data"
    assert paper.store.orders(ACCOUNT_ID) == ()


def test_pipeline_rejects_mixed_bar_scope(tmp_path: Path) -> None:
    bars = _bars()
    pipeline, paper = _pipeline(tmp_path)
    now = bars[-1].timestamp + bars[-1].timeframe.duration
    mixed_source = bars[-1].model_copy(update={"source": "other-source"})

    result = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=(*bars[:-1], mixed_source),
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=now,
        system_health=SystemHealthSnapshot(now, SystemHealthStatus.HEALTHY),
    )

    assert result.action == "NO_TRADE"
    assert result.health == "BLOCKED"
    assert result.reason_codes == ("DATA_SCOPE_MISMATCH",)
    assert len(result.trace) == 1
    assert result.trace[0].stage == "data"
    assert paper.store.orders(ACCOUNT_ID) == ()


def test_pipeline_rejects_future_dated_ingestion(tmp_path: Path) -> None:
    bars = _bars()
    pipeline, paper = _pipeline(tmp_path)
    now = bars[-1].timestamp + bars[-1].timeframe.duration
    future_ingestion = bars[-1].model_copy(
        update={"ingestion_timestamp": now + timedelta(minutes=1)}
    )

    result = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=(*bars[:-1], future_ingestion),
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=now,
        system_health=SystemHealthSnapshot(now, SystemHealthStatus.HEALTHY),
    )

    assert result.action == "NO_TRADE"
    assert result.health == "BLOCKED"
    assert result.reason_codes == ("DATA_INGESTION_FUTURE_DATED",)
    assert len(result.trace) == 1
    assert result.trace[0].stage == "data"
    assert paper.store.orders(ACCOUNT_ID) == ()


def test_pipeline_rejects_future_dated_ingestion_on_historical_bar(tmp_path: Path) -> None:
    bars = _bars()
    pipeline, paper = _pipeline(tmp_path)
    now = bars[-1].timestamp + bars[-1].timeframe.duration
    future_ingestion = bars[0].model_copy(
        update={"ingestion_timestamp": now + timedelta(minutes=1)}
    )

    result = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=(future_ingestion, *bars[1:]),
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=now,
        system_health=SystemHealthSnapshot(now, SystemHealthStatus.HEALTHY),
    )

    assert result.action == "NO_TRADE"
    assert result.health == "BLOCKED"
    assert result.reason_codes == ("DATA_INGESTION_FUTURE_DATED",)
    assert paper.store.orders(ACCOUNT_ID) == ()


def test_pipeline_rejects_future_dated_quote_on_historical_bar(tmp_path: Path) -> None:
    bars = _bars()
    pipeline, paper = _pipeline(tmp_path)
    now = bars[-1].timestamp + bars[-1].timeframe.duration
    future_quote = bars[0].model_copy(
        update={"quote_timestamp": now + timedelta(minutes=1)}
    )

    result = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=(future_quote, *bars[1:]),
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=now,
        system_health=SystemHealthSnapshot(now, SystemHealthStatus.HEALTHY),
    )

    assert result.action == "NO_TRADE"
    assert result.health == "BLOCKED"
    assert result.reason_codes == ("DATA_QUOTE_FUTURE_DATED",)
    assert paper.store.orders(ACCOUNT_ID) == ()


def test_safety_risk_veto_is_blocked_not_healthy(tmp_path: Path) -> None:
    bars = _bars()
    parameters = RiskFirewall().parameters.model_copy(
        update={"max_spread_fraction": Decimal("0.00001")}
    )
    pipeline, paper = _pipeline(tmp_path, RiskFirewall(parameters))
    now = bars[-1].timestamp + bars[-1].timeframe.duration

    result = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=bars,
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=now,
        system_health=SystemHealthSnapshot(now, SystemHealthStatus.HEALTHY),
    )

    assert result.action == "NO_TRADE"
    assert result.health == "BLOCKED"
    assert "RISK_SPREAD_TOO_WIDE" in result.reason_codes
    risk_event = next(event for event in result.trace if event.stage == "risk_firewall")
    failed_checks = [
        item for item in json.loads(dict(risk_event.metadata)["checks"]) if not item["passed"]
    ]
    assert any(
        item["check_id"] == "spread" and item["reason_code"] == "spread_too_wide"
        for item in failed_checks
    )
    assert json.loads(dict(risk_event.metadata)["authorization"]) is None
    assert paper.store.orders(ACCOUNT_ID) == ()


def test_missing_system_health_vetoes_without_creating_an_order(tmp_path: Path) -> None:
    bars = _bars()
    pipeline, paper = _pipeline(tmp_path)
    now = bars[-1].timestamp + bars[-1].timeframe.duration

    result = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=bars,
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=now,
    )

    assert result.action == "NO_TRADE"
    assert result.health == "BLOCKED"
    assert "RISK_SYSTEM_UNHEALTHY" in result.reason_codes
    assert paper.store.orders(ACCOUNT_ID) == ()


def test_stale_system_health_vetoes_without_creating_an_order(tmp_path: Path) -> None:
    bars = _bars()
    pipeline, paper = _pipeline(tmp_path)
    now = bars[-1].timestamp + bars[-1].timeframe.duration

    result = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=bars,
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=now,
        system_health=SystemHealthSnapshot(
            now - timedelta(minutes=6), SystemHealthStatus.HEALTHY
        ),
    )

    assert result.action == "NO_TRADE"
    assert result.health == "BLOCKED"
    assert "RISK_SYSTEM_HEALTH_STALE" in result.reason_codes
    assert paper.store.orders(ACCOUNT_ID) == ()


def test_quote_fresh_at_decision_cutoff_but_stale_at_processing_time_is_blocked(
    tmp_path: Path,
) -> None:
    bars = _bars()
    pipeline, paper = _pipeline(tmp_path)
    decision_time = bars[-1].timestamp + bars[-1].timeframe.duration
    processing_time = decision_time + timedelta(minutes=2)

    result = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=bars,
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=decision_time,
        processing_clock=lambda: processing_time,
        system_health=SystemHealthSnapshot(processing_time, SystemHealthStatus.HEALTHY),
    )

    assert result.action == "NO_TRADE"
    assert result.reason_codes == ("DATA_QUOTE_STALE",)
    assert paper.store.orders(ACCOUNT_ID) == ()


def test_ingestion_after_bar_close_by_decision_cutoff_is_admissible(tmp_path: Path) -> None:
    bars = _bars()
    pipeline, paper = _pipeline(tmp_path)
    decision_time = bars[-1].timestamp + bars[-1].timeframe.duration

    result = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=bars,
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=decision_time,
        system_health=SystemHealthSnapshot(decision_time, SystemHealthStatus.HEALTHY),
    )

    assert result.action == "ORDER_SUBMITTED"
    assert paper.store.orders(ACCOUNT_ID)


def test_processing_delay_rechecks_freshness_before_order_mutation(tmp_path: Path) -> None:
    bars = _bars()
    pipeline, paper = _pipeline(tmp_path)
    decision_time = bars[-1].timestamp + bars[-1].timeframe.duration
    processing_times = iter(
        (decision_time, decision_time, decision_time + timedelta(minutes=2))
    )

    result = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=bars,
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=decision_time,
        processing_clock=lambda: next(processing_times),
        system_health=SystemHealthSnapshot(decision_time, SystemHealthStatus.HEALTHY),
    )

    assert result.action == "NO_TRADE"
    assert result.reason_codes == ("DATA_QUOTE_STALE_AT_EXECUTION",)
    assert paper.store.orders(ACCOUNT_ID) == ()


def test_later_decision_processes_pending_order_once(tmp_path: Path) -> None:
    initial_bars = _bars()
    pipeline, paper = _pipeline(tmp_path)
    first_timestamp = initial_bars[-1].timestamp + initial_bars[-1].timeframe.duration

    first = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=initial_bars,
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=first_timestamp,
        system_health=SystemHealthSnapshot(first_timestamp, SystemHealthStatus.HEALTHY),
    )
    assert first.action == "ORDER_SUBMITTED"
    assert first.order is not None

    next_bar_timestamp = initial_bars[-1].timestamp + timedelta(hours=1)
    next_close = initial_bars[-1].close + Decimal("0.0001")
    next_bar = MarketBar(
        instrument=INSTRUMENT,
        timeframe=Timeframe.H1,
        timestamp=next_bar_timestamp,
        open=next_close - Decimal("0.00005"),
        high=next_close + Decimal("0.00010"),
        low=next_close - Decimal("0.00010"),
        close=next_close,
        source="synthetic-fixture",
        ingestion_timestamp=next_bar_timestamp + timedelta(hours=1),
        bid=next_close - Decimal("0.00005"),
        ask=next_close + Decimal("0.00005"),
        quote_timestamp=next_bar_timestamp + timedelta(hours=1),
    )
    second_timestamp = next_bar.timestamp + next_bar.timeframe.duration
    second = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=(*initial_bars, next_bar),
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=second_timestamp,
        system_health=SystemHealthSnapshot(second_timestamp, SystemHealthStatus.HEALTHY),
    )

    assert len(second.fills) == 1
    # The allocator preserves the unfilled remainder as a new bounded request
    # after the first original authorization expires at the adverse fill.
    assert len(paper.store.orders(ACCOUNT_ID)) == 2
    assert len(paper.store.fills(ACCOUNT_ID)) == 1


def test_pending_order_is_managed_when_strategy_becomes_ineligible(tmp_path: Path) -> None:
    initial_bars = _bars()
    pipeline, paper = _pipeline(tmp_path)
    first_timestamp = initial_bars[-1].timestamp + initial_bars[-1].timeframe.duration

    first = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=initial_bars,
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=first_timestamp,
        system_health=SystemHealthSnapshot(first_timestamp, SystemHealthStatus.HEALTHY),
    )
    assert first.action == "ORDER_SUBMITTED"

    next_timestamp = initial_bars[-1].timestamp + timedelta(hours=1)
    next_close = initial_bars[-1].close + Decimal("0.0001")
    next_bar = initial_bars[-1].model_copy(
        update={
            "timestamp": next_timestamp,
            "open": next_close - Decimal("0.00005"),
            "high": next_close + Decimal("0.00010"),
            "low": next_close - Decimal("0.00010"),
            "close": next_close,
            "ingestion_timestamp": next_timestamp + timedelta(hours=1),
            "bid": next_close - Decimal("0.00005"),
            "ask": next_close + Decimal("0.00005"),
            "quote_timestamp": next_timestamp + timedelta(hours=1),
        }
    )
    pipeline.governance.explain_strategy_eligibility = lambda strategy_id, version: (  # type: ignore[attr-defined]
        SimpleNamespace(operationally_eligible=False, reasons=("STRATEGY_DISABLED",))
    )
    second_timestamp = next_bar.timestamp + next_bar.timeframe.duration

    second = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=(*initial_bars, next_bar),
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=second_timestamp,
        system_health=SystemHealthSnapshot(second_timestamp, SystemHealthStatus.HEALTHY),
    )

    assert second.action == "NO_TRADE"
    assert second.health == "BLOCKED"
    assert "forex_trend_following@1:STRATEGY_DISABLED" in second.reason_codes
    assert len(second.fills) == 1
    assert len(paper.store.fills(ACCOUNT_ID)) == 1


def test_governed_fixture_rejects_runtime_identity_mismatch(tmp_path: Path) -> None:
    bars = _bars()
    pipeline, paper = _pipeline(tmp_path)
    now = bars[-1].timestamp + bars[-1].timeframe.duration
    strategy = ForexTrendFollowingStrategy()
    mismatch = StrategyRuntimeBinding(
        implementation_id="forex_trend_following",
        implementation_version="1",
        code_identity="wrong-code-identity",
        strategy=strategy,
    )
    blocked_pipeline = PaperDecisionPipeline(
        paper_engine=paper,
        governance=pipeline.governance,
        runtime_bindings=(mismatch,),
        regime_detector=EmaPercentileRegimeDetector(),
        fusion_policy=MajorityVoteFusionPolicy(),
        risk_firewall=RiskFirewall(),
    )

    result = blocked_pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=bars,
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=now,
        system_health=SystemHealthSnapshot(now, SystemHealthStatus.HEALTHY),
    )

    assert result.health == "BLOCKED"
    assert "forex_trend_following@1:STRATEGY_CODE_IDENTITY_MISMATCH" in result.reason_codes
    assert paper.store.orders(ACCOUNT_ID) == ()


def test_old_artifact_cannot_authorize_changed_content_at_same_runtime_locator(
    tmp_path: Path, monkeypatch
) -> None:
    bars = _bars()
    pipeline, paper = _pipeline(tmp_path)
    now = bars[-1].timestamp + bars[-1].timeframe.duration
    strategy = ForexTrendFollowingStrategy()
    old_identity = runtime_content_identity(strategy)
    old_artifact = _artifact(strategy)
    original_module_source = runtime_identity_module._module_source

    def changed_module_source(module):
        path, content = original_module_source(module)
        if module.__name__ == "traderos.strategies.baselines":
            content += b"\n# implementation content changed\n"
        return path, content

    monkeypatch.setattr(runtime_identity_module, "_module_source", changed_module_source)
    changed_identity = runtime_content_identity(strategy)
    assert changed_identity != old_identity
    changed_binding = StrategyRuntimeBinding(
        implementation_id="forex_trend_following",
        implementation_version="1",
        code_identity=changed_identity,
        strategy=strategy,
    )
    changed_pipeline = PaperDecisionPipeline(
        paper_engine=paper,
        governance=cast(
            StrategyGovernanceService,
            SimpleNamespace(
                explain_strategy_eligibility=lambda strategy_id, version: SimpleNamespace(
                    operationally_eligible=True, reasons=()
                ),
                get_strategy_artifact=lambda strategy_id, version: old_artifact,
            ),
        ),
        runtime_bindings=(changed_binding,),
        regime_detector=EmaPercentileRegimeDetector(),
        fusion_policy=MajorityVoteFusionPolicy(),
        risk_firewall=RiskFirewall(),
    )

    result = changed_pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=bars,
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="synthetic-fixture-dataset",
        now=now,
        system_health=SystemHealthSnapshot(now, SystemHealthStatus.HEALTHY),
    )

    assert result.action == "NO_TRADE"
    assert "forex_trend_following@1:STRATEGY_CODE_IDENTITY_MISMATCH" in result.reason_codes
    assert old_artifact.code_identity == old_identity
    assert paper.store.orders(ACCOUNT_ID) == ()


def test_governed_fixture_rejects_dataset_identity_mismatch(tmp_path: Path) -> None:
    bars = _bars()
    pipeline, paper = _pipeline(tmp_path)
    now = bars[-1].timestamp + bars[-1].timeframe.duration

    result = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=bars,
        dataset_version="synthetic-fixture-dataset",
        dataset_hash="different-synthetic-dataset",
        now=now,
        system_health=SystemHealthSnapshot(now, SystemHealthStatus.HEALTHY),
    )

    assert result.health == "BLOCKED"
    assert "forex_trend_following@1:STRATEGY_DATASET_IDENTITY_MISMATCH" in result.reason_codes
    assert paper.store.orders(ACCOUNT_ID) == ()


def test_governed_fixture_requires_dataset_identity_when_artifact_binds_one(
    tmp_path: Path,
) -> None:
    bars = _bars()
    pipeline, paper = _pipeline(tmp_path)
    now = bars[-1].timestamp + bars[-1].timeframe.duration

    result = pipeline.run(
        account_id=ACCOUNT_ID,
        strategy_keys=(("forex_trend_following", "1"),),
        bars=bars,
        dataset_version="synthetic-fixture-dataset",
        now=now,
        system_health=SystemHealthSnapshot(now, SystemHealthStatus.HEALTHY),
    )

    assert result.health == "BLOCKED"
    assert "forex_trend_following@1:STRATEGY_DATASET_IDENTITY_UNAVAILABLE" in result.reason_codes
    assert paper.store.orders(ACCOUNT_ID) == ()
