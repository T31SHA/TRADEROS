"""Phase 9 research governance and causal-validation invariants."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from traderos.backtesting import (
    AlwaysFlatStrategy,
    BacktestConfig,
    CostConfig,
    SlippageConfig,
    SpreadConfig,
)
from traderos.data.bars import MarketBar
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.lineage import AdjustmentPolicy
from traderos.data.timeframes import Timeframe
from traderos.research.execution import execute_backtest
from traderos.research.models import (
    CostScenario,
    DatasetManifest,
    ExperimentSpec,
    OosContaminationError,
    ResearchError,
    ResearchPlan,
    ResearchScope,
    ResearchSplit,
    TemporalRange,
    WalkForwardConfig,
    WalkForwardFold,
    WalkForwardMode,
)

BASE = datetime(2024, 1, 2, tzinfo=UTC)


def _bars(count: int = 16) -> list[MarketBar]:
    instrument = Instrument(
        canonical_symbol="AAPL",
        asset_class=AssetClass.EQUITY,
        exchange="NASDAQ",
        trading_currency="USD",
        provider_symbols={"fixture": "AAPL"},
    )
    return [
        MarketBar(
            instrument=instrument,
            timeframe=Timeframe.H1,
            adjustment_policy=AdjustmentPolicy.RAW,
            timestamp=BASE + timedelta(hours=index),
            open=Decimal("100") + index,
            high=Decimal("101") + index,
            low=Decimal("99") + index,
            close=Decimal("100") + index,
            volume=Decimal("1000"),
            source="fixture",
            currency="USD",
            ingestion_timestamp=BASE,
        )
        for index in range(count)
    ]


def _manifest() -> DatasetManifest:
    return DatasetManifest.from_bars(
        dataset_version="research-fixture-v1",
        bars=_bars(),
        quality_status="verified_fixture",
        historical_membership_available=False,
        delistings_available=False,
        corporate_actions_verified=False,
    )


def _plan() -> ResearchPlan:
    split = ResearchSplit(
        development=TemporalRange(BASE, BASE + timedelta(hours=10)),
        locked_out_of_sample=TemporalRange(BASE + timedelta(hours=10), BASE + timedelta(hours=16)),
        split_id="fixture-split-v1",
    )
    walk_forward = WalkForwardConfig(
        mode=WalkForwardMode.EXPANDING,
        initial_train=TemporalRange(BASE, BASE + timedelta(hours=4)),
        forward_duration=timedelta(hours=2),
        fold_count=3,
        locked_oos_start=split.locked_out_of_sample.start,
    )
    return ResearchPlan(split=split, dataset=_manifest(), walk_forward=walk_forward)


def _config(*, end: datetime = BASE + timedelta(hours=2)) -> BacktestConfig:
    return BacktestConfig(
        dataset_version="research-fixture-v1",
        instrument_symbols=("AAPL",),
        timeframe=Timeframe.H1,
        start=BASE,
        end=end,
        starting_cash=Decimal("1000"),
        account_currency="USD",
        strategy_id="always_flat",
        strategy_version="1",
        commission=CostConfig(per_unit=Decimal("0.1"), rate=Decimal("0.01"), minimum=Decimal("1")),
        spread=SpreadConfig(fallback_absolute=Decimal("0.02")),
        slippage=SlippageConfig(absolute=Decimal("0.03")),
    )


def _spec(
    plan: ResearchPlan,
    *,
    scope: ResearchScope = ResearchScope.DEVELOPMENT,
    period: TemporalRange | None = None,
    selection_eligible: bool = True,
    frozen_from_experiment_id: str | None = None,
    backtest_experiment_id: str = "backtest-fixture",
) -> ExperimentSpec:
    return ExperimentSpec.from_parameters(
        dataset=plan.dataset,
        split_id=plan.split.split_id,
        scope=scope,
        period=period or TemporalRange(BASE, BASE + timedelta(hours=2)),
        strategy_id="always_flat",
        strategy_version="1",
        parameters={"z": 2, "a": 1},
        feature_versions=(),
        regime_configuration_id=None,
        fusion_configuration_id=None,
        risk_policy_configuration_id=None,
        backtest_experiment_id=backtest_experiment_id,
        cost_scenario_id="base",
        code_version="test-code",
        random_seed=17,
        selection_eligible=selection_eligible,
        frozen_from_experiment_id=frozen_from_experiment_id,
    )


def test_dataset_manifest_is_order_invariant_and_hashes_execution_relevant_fields() -> None:
    original = _bars()
    reversed_manifest = DatasetManifest.from_bars(
        dataset_version="research-fixture-v1",
        bars=list(reversed(original)),
        quality_status="verified_fixture",
        historical_membership_available=False,
        delistings_available=False,
        corporate_actions_verified=False,
    )
    changed = original.copy()
    changed[0] = changed[0].model_copy(update={"trade_count": 3})
    changed_manifest = DatasetManifest.from_bars(
        dataset_version="research-fixture-v1",
        bars=changed,
        quality_status="verified_fixture",
        historical_membership_available=False,
        delistings_available=False,
        corporate_actions_verified=False,
    )

    assert _manifest().dataset_hash == reversed_manifest.dataset_hash
    assert _manifest().dataset_hash != changed_manifest.dataset_hash
    assert _manifest().equity_bias_unresolved is True


def test_dataset_manifest_rejects_mixed_sources_that_would_break_single_source_lineage() -> None:
    mixed = _bars()
    mixed[1] = mixed[1].model_copy(update={"source": "other"})

    with pytest.raises(ResearchError, match="cannot mix"):
        DatasetManifest.from_bars(
            dataset_version="research-fixture-v1",
            bars=mixed,
            quality_status="verified_fixture",
            historical_membership_available=False,
            delistings_available=False,
            corporate_actions_verified=False,
        )


def test_temporal_split_and_walk_forward_are_monotonic_and_never_enter_locked_oos() -> None:
    plan = _plan()
    folds = plan.walk_forward.folds()

    assert [(fold.train.end, fold.test.start) for fold in folds] == [
        (BASE + timedelta(hours=4), BASE + timedelta(hours=4)),
        (BASE + timedelta(hours=6), BASE + timedelta(hours=6)),
        (BASE + timedelta(hours=8), BASE + timedelta(hours=8)),
    ]
    assert all(fold.test.end <= plan.split.development.end for fold in folds)
    assert all(fold.test.end <= plan.split.locked_out_of_sample.start for fold in folds)

    with pytest.raises(ResearchError, match="enter locked OOS"):
        WalkForwardConfig(
            mode=WalkForwardMode.EXPANDING,
            initial_train=TemporalRange(BASE, BASE + timedelta(hours=8)),
            forward_duration=timedelta(hours=2),
            fold_count=2,
            locked_oos_start=BASE + timedelta(hours=10),
        ).folds()


def test_temporal_and_walk_forward_contracts_reject_invalid_ranges_and_windows() -> None:
    with pytest.raises(ResearchError, match="must precede"):
        TemporalRange(BASE, BASE)
    period = TemporalRange(BASE, BASE + timedelta(hours=2))
    assert period.contains(BASE) is True
    assert period.contains(period.end) is False
    with pytest.raises(ResearchError, match="identity"):
        ResearchSplit(period, TemporalRange(period.end, period.end + timedelta(hours=1)), " ")
    with pytest.raises(ResearchError, match="must not overlap"):
        ResearchSplit(
            period,
            TemporalRange(BASE + timedelta(hours=1), BASE + timedelta(hours=3)),
            "overlap",
        )
    split = ResearchSplit(
        period,
        TemporalRange(period.end, period.end + timedelta(hours=2)),
        "valid",
    )
    with pytest.raises(ResearchError, match="crosses"):
        split.scope_for(TemporalRange(BASE + timedelta(hours=1), BASE + timedelta(hours=3)))
    with pytest.raises(ResearchError, match="requires a positive"):
        WalkForwardConfig(
            WalkForwardMode.ROLLING,
            period,
            timedelta(hours=1),
            1,
            BASE + timedelta(hours=3),
        )
    with pytest.raises(ResearchError, match="must not set"):
        WalkForwardConfig(
            WalkForwardMode.EXPANDING,
            period,
            timedelta(hours=1),
            1,
            BASE + timedelta(hours=3),
            rolling_train_duration=timedelta(hours=1),
        )
    with pytest.raises(ResearchError, match="predates"):
        WalkForwardConfig(
            WalkForwardMode.ROLLING,
            TemporalRange(BASE, BASE + timedelta(hours=2)),
            timedelta(hours=1),
            1,
            BASE + timedelta(hours=5),
            rolling_train_duration=timedelta(hours=3),
        ).folds()
    for forward_duration, fold_count, locked_start, message in (
        (timedelta(0), 1, BASE + timedelta(hours=3), "duration"),
        (timedelta(hours=1), 0, BASE + timedelta(hours=3), "fold count"),
        (timedelta(hours=1), 1, period.end, "initial training"),
    ):
        with pytest.raises(ResearchError, match=message):
            WalkForwardConfig(
                WalkForwardMode.EXPANDING,
                period,
                forward_duration,
                fold_count,
                locked_start,
            )
    with pytest.raises(ResearchError, match="non-negative"):
        WalkForwardFold(-1, period, TemporalRange(period.end, period.end + timedelta(hours=1)))
    with pytest.raises(ResearchError, match="must not overlap"):
        WalkForwardFold(
            0, period, TemporalRange(BASE + timedelta(hours=1), BASE + timedelta(hours=3))
        )


def test_locked_oos_cannot_be_selected_and_requires_frozen_development_lineage() -> None:
    plan = _plan()
    oos_period = plan.split.locked_out_of_sample
    with pytest.raises(OosContaminationError):
        _spec(
            plan,
            scope=ResearchScope.LOCKED_OUT_OF_SAMPLE,
            period=oos_period,
            selection_eligible=True,
            frozen_from_experiment_id="research-development",
        )
    with pytest.raises(ResearchError, match="requires a frozen"):
        _spec(
            plan,
            scope=ResearchScope.LOCKED_OUT_OF_SAMPLE,
            period=oos_period,
            selection_eligible=False,
        )

    frozen_oos = _spec(
        plan,
        scope=ResearchScope.LOCKED_OUT_OF_SAMPLE,
        period=oos_period,
        selection_eligible=False,
        frozen_from_experiment_id="research-development",
    )
    plan.validate_experiment(frozen_oos)
    with pytest.raises(ResearchError, match="outside development"):
        plan.validate_experiment(_spec(plan, scope=ResearchScope.WALK_FORWARD, period=oos_period))


def test_experiment_identity_is_mapping_order_invariant_and_cost_stress_never_mutates_base() -> (
    None
):
    plan = _plan()
    first = _spec(plan)
    second = ExperimentSpec.from_parameters(**{**first.__dict__, "parameters": {"a": 1, "z": 2}})
    base = _config()
    stressed = CostScenario("cost-2x", Decimal("2")).apply(base)

    assert first.parameters == (("a", "1"), ("z", "2"))
    assert first.experiment_id == second.experiment_id
    assert base.commission.per_unit == Decimal("0.1")
    assert stressed.commission.per_unit == Decimal("0.2")
    assert stressed.spread.fallback_absolute == Decimal("0.04")
    assert stressed.slippage.absolute == Decimal("0.06")


def test_manifest_cost_and_experiment_validation_reject_unsafe_lineage() -> None:
    with pytest.raises(ResearchError, match="at least one bar"):
        DatasetManifest.from_bars(
            dataset_version="v1",
            bars=(),
            quality_status="fixture",
            historical_membership_available=True,
            delistings_available=True,
            corporate_actions_verified=True,
        )
    duplicate = _bars(2)
    duplicate.append(duplicate[0])
    with pytest.raises(ResearchError, match="unique logical"):
        DatasetManifest.from_bars(
            dataset_version="v1",
            bars=duplicate,
            quality_status="fixture",
            historical_membership_available=True,
            delistings_available=True,
            corporate_actions_verified=True,
        )
    with pytest.raises(ResearchError, match="identity"):
        CostScenario("", Decimal("1"))
    with pytest.raises(ResearchError, match="non-negative"):
        CostScenario("bad", Decimal("-1"))
    plan = _plan()
    with pytest.raises(ResearchError, match="version"):
        DatasetManifest(
            dataset_version=" ",
            dataset_hash="hash",
            symbols=("AAPL",),
            timeframe=Timeframe.H1,
            period=plan.dataset.period,
            adjustment_policy=AdjustmentPolicy.RAW,
            source="fixture",
            quality_status="fixture",
            historical_membership_available=True,
            delistings_available=True,
            corporate_actions_verified=True,
        )
    with pytest.raises(ResearchError, match="source"):
        DatasetManifest(
            dataset_version="v1",
            dataset_hash="hash",
            symbols=("AAPL",),
            timeframe=Timeframe.H1,
            period=plan.dataset.period,
            adjustment_policy=AdjustmentPolicy.RAW,
            source=" ",
            quality_status="fixture",
            historical_membership_available=True,
            delistings_available=True,
            corporate_actions_verified=True,
        )
    with pytest.raises(ResearchError, match="symbols"):
        DatasetManifest(
            dataset_version="v1",
            dataset_hash="hash",
            symbols=("",),
            timeframe=Timeframe.H1,
            period=plan.dataset.period,
            adjustment_policy=AdjustmentPolicy.RAW,
            source="fixture",
            quality_status="fixture",
            historical_membership_available=True,
            delistings_available=True,
            corporate_actions_verified=True,
        )
    with pytest.raises(ResearchError, match="unique"):
        DatasetManifest(
            dataset_version="v1",
            dataset_hash="hash",
            symbols=("AAPL", "AAPL"),
            timeframe=Timeframe.H1,
            period=plan.dataset.period,
            adjustment_policy=AdjustmentPolicy.RAW,
            source="fixture",
            quality_status="fixture",
            historical_membership_available=True,
            delistings_available=True,
            corporate_actions_verified=True,
        )
    with pytest.raises(ResearchError, match="parameter names"):
        ExperimentSpec(
            dataset=plan.dataset,
            split_id=plan.split.split_id,
            scope=ResearchScope.DEVELOPMENT,
            period=TemporalRange(BASE, BASE + timedelta(hours=2)),
            strategy_id="fixture",
            strategy_version="1",
            parameters=(("x", "1"), ("x", "2")),
            feature_versions=(),
            regime_configuration_id=None,
            fusion_configuration_id=None,
            risk_policy_configuration_id=None,
            backtest_experiment_id="phase3",
            cost_scenario_id="base",
            code_version="test",
            random_seed=None,
            selection_eligible=True,
        )


def test_research_plan_rejects_boundary_dataset_and_experiment_mismatches() -> None:
    plan = _plan()
    with pytest.raises(ResearchError, match="disagree"):
        ResearchPlan(
            split=plan.split,
            dataset=plan.dataset,
            walk_forward=WalkForwardConfig(
                WalkForwardMode.EXPANDING,
                TemporalRange(BASE, BASE + timedelta(hours=4)),
                timedelta(hours=2),
                1,
                BASE + timedelta(hours=11),
            ),
        )
    alternate_dataset = DatasetManifest(
        dataset_version="other",
        dataset_hash="hash",
        symbols=("AAPL",),
        timeframe=Timeframe.H1,
        period=plan.dataset.period,
        adjustment_policy=AdjustmentPolicy.RAW,
        source="fixture",
        quality_status="fixture",
        historical_membership_available=True,
        delistings_available=True,
        corporate_actions_verified=True,
    )
    with pytest.raises(ResearchError, match="dataset differs"):
        plan.validate_experiment(
            ExperimentSpec.from_parameters(
                **{**_spec(plan).__dict__, "dataset": alternate_dataset, "parameters": {"a": 1}}
            )
        )
    with pytest.raises(ResearchError, match="split identity"):
        plan.validate_experiment(
            ExperimentSpec.from_parameters(
                **{**_spec(plan).__dict__, "split_id": "other", "parameters": {"a": 1}}
            )
        )


def test_execute_backtest_requires_frozen_lineage_before_reusing_phase3_engine() -> None:
    plan = _plan()
    config = _config()
    spec = _spec(plan, backtest_experiment_id=config.experiment_id)

    result = execute_backtest(
        plan=plan,
        spec=spec,
        config=config,
        bars=_bars(2),
        strategy=AlwaysFlatStrategy(),
    )
    assert result.experiment_id == config.experiment_id
    assert result.metrics.trade_count == 0

    wrong_spec = _spec(plan, backtest_experiment_id="not-the-config")
    with pytest.raises(ResearchError, match="configuration identity"):
        execute_backtest(
            plan=plan,
            spec=wrong_spec,
            config=config,
            bars=_bars(2),
            strategy=AlwaysFlatStrategy(),
        )

    for bad_config, message in (
        (config.model_copy(update={"dataset_version": "other"}), "dataset version"),
        (config.model_copy(update={"end": BASE + timedelta(hours=3)}), "period"),
        (config.model_copy(update={"strategy_id": "other"}), "strategy"),
    ):
        bad_spec = _spec(plan, backtest_experiment_id=bad_config.experiment_id)
        with pytest.raises(ResearchError, match=message):
            execute_backtest(
                plan=plan,
                spec=bad_spec,
                config=bad_config,
                bars=_bars(2),
                strategy=AlwaysFlatStrategy(),
            )
    with pytest.raises(ResearchError, match="strategy object"):
        execute_backtest(
            plan=plan,
            spec=spec,
            config=config,
            bars=_bars(2),
            strategy=AlwaysFlatStrategy(strategy_id="other"),
        )
