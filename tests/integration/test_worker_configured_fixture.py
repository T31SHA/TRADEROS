"""SOFTWARE DEMONSTRATION / SYNTHETIC FIXTURE for the configured worker.

This test exercises the real worker, local data store, paper account, and
governance service in an isolated SQLite database. The candidate artifact is
deliberately not operationally eligible: no evidence or approval is attached,
and no empirical result is registered.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from traderos.core.config import Settings, TradingMode
from traderos.data.bars import MarketBar
from traderos.data.hashing import market_bar_content_hash
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.lineage import AdjustmentPolicy, DatasetMetadata
from traderos.data.timeframes import Timeframe
from traderos.research.strategy_lifecycle import (
    RegisteredImplementation,
    StrategyArtifact,
)
from traderos.worker import PaperWorker

ACCOUNT_ID = "synthetic-worker-account"
DATASET_VERSION = "synthetic-worker-dataset-v1"
INSTRUMENT = Instrument(
    canonical_symbol="EUR/USD",
    asset_class=AssetClass.FOREX,
    base_currency="EUR",
    quote_currency="USD",
)


def _candidate() -> StrategyArtifact:
    return StrategyArtifact(
        strategy_id="fixture_strategy",
        strategy_version="1",
        family_id="synthetic-worker-family",
        author="software-test",
        implementation=RegisteredImplementation("always_flat", "1"),
        instruments=(INSTRUMENT.canonical_symbol,),
        timeframes=(Timeframe.H1.value,),
        parameters=(),
        features=(),
        training_reference=None,
        validation_reference=None,
        oos_reference=None,
        forecast_semantics="synthetic fixture only",
        holding_period=None,
        turnover_assumption=None,
        cost_model_reference=None,
        capacity_model_reference=None,
        regime_dependencies=(),
        risk_dependencies=("risk_firewall.v1",),
        known_failure_modes=("synthetic fixture",),
        code_identity="fixture:always-flat",
        dataset_identity=None,
        configuration_identity=None,
        environment_reference="pytest",
    )


def test_configured_worker_blocks_candidate_before_any_order(tmp_path: Path) -> None:
    now = datetime(2026, 1, 5, 12, tzinfo=UTC)
    settings = Settings(
        _env_file=None,
        trading_mode=TradingMode.PAPER,
        database_url=f"sqlite:///{tmp_path / 'worker.db'}",
        worker_account_id=ACCOUNT_ID,
        worker_instrument=INSTRUMENT.canonical_symbol,
        worker_timeframe=Timeframe.H1.value,
        worker_data_source="fixture",
        worker_dataset_version=DATASET_VERSION,
        worker_strategy_versions="fixture_strategy@1",
    )
    worker = PaperWorker.from_settings(settings)
    worker._clock = lambda: now

    firewall = worker.dependencies.pipeline.risk_firewall  # type: ignore[union-attr]
    worker.dependencies.paper_engine.create_account(
        account_id=ACCOUNT_ID,
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
                instrument=INSTRUMENT,
                timeframe=Timeframe.H1,
                timestamp=now - timedelta(hours=1),
                open=Decimal("1.1000"),
                high=Decimal("1.1100"),
                low=Decimal("1.0900"),
                close=Decimal("1.1050"),
                source="fixture",
                ingestion_timestamp=now,
                bid=Decimal("1.1049"),
                ask=Decimal("1.1051"),
                quote_timestamp=now,
                adjustment_policy=AdjustmentPolicy.RAW,
            ),
        )
    )
    fixture_bar = worker.dependencies.market_store.query_bars(
        INSTRUMENT.canonical_symbol,
        Timeframe.H1,
        now - timedelta(hours=1),
        now,
        source="fixture",
        adjustment_policy=AdjustmentPolicy.RAW,
    )
    worker.dependencies.market_store.record_dataset(
        DatasetMetadata(
            dataset_version=DATASET_VERSION,
            dataset_hash=market_bar_content_hash(fixture_bar),
            provider="fixture",
            symbol=INSTRUMENT.canonical_symbol,
            timeframe=Timeframe.H1.value,
            start=now - timedelta(hours=1),
            end=now,
            adjustment_policy=AdjustmentPolicy.RAW,
            configuration_version="synthetic-worker-test",
            quality_status="clean",
            created_at=now,
        )
    )
    worker.dependencies.governance.store.register_artifact(  # type: ignore[union-attr]
        _candidate(), registered_at=now
    )

    status = worker.run_once()

    assert status.health.value == "BLOCKED", status.last_error
    assert status.data_status == "BLOCKED"
    assert status.data_reason == "DATA_FIXTURE_NOT_OPERATIONAL"
    assert status.reconciliation_status == "HEALTHY"
    assert status.eligible_strategy_count == 0
    assert "fixture_strategy@1:NOT_OPERATIONAL_STAGE" in status.last_reason_codes
    assert status.last_decision == "NO_TRADE"
    assert worker.dependencies.paper_engine.store.orders(ACCOUNT_ID) == ()
    assert worker.dependencies.paper_engine.store.fills(ACCOUNT_ID) == ()
