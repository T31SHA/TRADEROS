"""Research artifacts are data-only and reject executable object injection."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from traderos.data.bars import MarketBar
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.timeframes import Timeframe
from traderos.research.evidence import EvidencePackage
from traderos.research.models import (
    DatasetManifest,
    ExperimentSpec,
    ResearchError,
    ResearchScope,
    TemporalRange,
)


def test_evidence_export_rejects_executable_values() -> None:
    now = datetime(2024, 1, 1, tzinfo=UTC)
    instrument = Instrument(
        canonical_symbol="EUR/USD",
        asset_class=AssetClass.FOREX,
        base_currency="EUR",
        quote_currency="USD",
        trading_currency="USD",
        provider_symbols={"x": "x"},
    )
    bar = MarketBar(
        instrument=instrument,
        timeframe=Timeframe.H1,
        timestamp=now,
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        source="fixture",
        ingestion_timestamp=now,
    )
    dataset = DatasetManifest.from_bars(
        dataset_version="v1",
        bars=[bar],
        quality_status="fixture",
        historical_membership_available=True,
        delistings_available=True,
        corporate_actions_verified=True,
    )
    spec = ExperimentSpec.from_parameters(
        dataset=dataset,
        split_id="split",
        scope=ResearchScope.DEVELOPMENT,
        period=TemporalRange(now, now + timedelta(hours=1)),
        strategy_id="s",
        strategy_version="1",
        parameters={},
        feature_versions=(),
        regime_configuration_id=None,
        fusion_configuration_id=None,
        risk_policy_configuration_id=None,
        backtest_experiment_id="phase3",
        cost_scenario_id="base",
        code_version="test",
        random_seed=1,
        selection_eligible=True,
    )
    package = EvidencePackage(
        experiment=spec,
        dataset=dataset,
        configuration={"unsafe": lambda: None},
        folds=(),
        metrics={},
        trade_summary={},
        cost_attribution={},
        regime_breakdown={},
        robustness={},
        monte_carlo={},
        statistical_tests={},
        multiple_testing={},
        warnings=(),
        promotion_decision="hold",
        lineage={},
    )
    with pytest.raises(ResearchError, match="executable"):
        package.to_json()
