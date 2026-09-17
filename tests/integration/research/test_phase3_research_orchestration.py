"""Phase 9 orchestrates the authoritative Phase 3 simulator end to end."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from traderos.backtesting import AlwaysFlatStrategy, BacktestConfig
from traderos.data.bars import MarketBar
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.lineage import AdjustmentPolicy
from traderos.data.timeframes import Timeframe
from traderos.research import (
    DatasetManifest,
    PromotionDecision,
    QualificationPolicy,
    ResearchHypothesis,
    ResearchPlan,
    ResearchScope,
    ResearchSplit,
    ResearchValidationEngine,
    TemporalRange,
    WalkForwardConfig,
    WalkForwardMode,
)

BASE = datetime(2024, 1, 2, tzinfo=UTC)


def test_qualified_candidate_runs_through_phase3_and_exports_deterministic_evidence() -> None:
    instrument = Instrument(
        canonical_symbol="EUR/USD",
        asset_class=AssetClass.FOREX,
        base_currency="EUR",
        quote_currency="USD",
        trading_currency="USD",
        provider_symbols={"fixture": "EURUSD"},
    )
    bars = [
        MarketBar(
            instrument=instrument,
            timeframe=Timeframe.H1,
            adjustment_policy=AdjustmentPolicy.RAW,
            timestamp=BASE + timedelta(hours=index),
            open=Decimal("1.10"),
            high=Decimal("1.11"),
            low=Decimal("1.09"),
            close=Decimal("1.10"),
            bid=Decimal("1.099"),
            ask=Decimal("1.101"),
            source="fixture",
            ingestion_timestamp=BASE,
        )
        for index in range(8)
    ]
    manifest = DatasetManifest.from_bars(
        dataset_version="orchestration-v1",
        bars=bars,
        quality_status="fixture",
        historical_membership_available=True,
        delistings_available=True,
        corporate_actions_verified=True,
    )
    split = ResearchSplit(
        TemporalRange(BASE, BASE + timedelta(hours=6)),
        TemporalRange(BASE + timedelta(hours=6), BASE + timedelta(hours=8)),
        "split-v1",
    )
    plan = ResearchPlan(
        split,
        manifest,
        WalkForwardConfig(
            WalkForwardMode.EXPANDING,
            TemporalRange(BASE, BASE + timedelta(hours=2)),
            timedelta(hours=2),
            2,
            split.locked_out_of_sample.start,
        ),
    )
    config = BacktestConfig(
        dataset_version=manifest.dataset_version,
        instrument_symbols=("EUR/USD",),
        timeframe=Timeframe.H1,
        start=split.development.start,
        end=split.development.end,
        starting_cash=Decimal("1000"),
        account_currency="USD",
        strategy_id="always_flat",
        strategy_version="1",
    )
    engine = ResearchValidationEngine()
    qualified = engine.qualify_dataset(
        manifest=manifest,
        bars=bars,
        policy=QualificationPolicy("fixture", "1", require_quotes=True),
        checked_at=BASE,
    )
    hypothesis = ResearchHypothesis.create(
        hypothesis_id="hypothesis-1",
        name="no trade fixture",
        description="deterministic plumbing test",
        asset_class="forex",
        instrument_scope=("EUR/USD",),
        timeframe_scope=("1h",),
        economic_rationale="test only",
        expected_market_regime="active",
        candidate_strategy="always_flat",
        candidate_parameters={},
        feature_dependencies=(),
        cost_assumptions="Phase 3 defaults",
        risk_assumptions="recorded configuration",
        research_owner="test",
        created_at=BASE,
        version="1",
    )
    candidate = engine.create_candidate(
        dataset=qualified,
        hypothesis=hypothesis,
        family_id="family-1",
        strategy_id="always_flat",
        strategy_version="1",
        parameters={},
        feature_versions=(),
        regime_configuration_id="regime-fixture",
        fusion_configuration_id="fusion-fixture",
        risk_configuration_id="risk-fixture",
        creation_experiment_id="creation-1",
    )
    spec = engine.create_experiment(
        plan=plan,
        dataset=qualified,
        candidate=candidate,
        config=config,
        scope=ResearchScope.DEVELOPMENT,
        code_version="test",
        cost_scenario_id="base",
        random_seed=11,
        selection_eligible=True,
    )
    result = engine.run_backtest(
        plan=plan,
        spec=spec,
        config=config,
        bars=bars[:6],
        strategy=AlwaysFlatStrategy(),
    )
    package = engine.build_evidence_package(
        spec=spec,
        configuration={"phase3_config_id": config.experiment_id},
        result=result,
        promotion=PromotionDecision.HOLD,
    )
    assert result.experiment_id == config.experiment_id
    assert result.event_count == 6
    assert package.to_json() == package.to_json()
