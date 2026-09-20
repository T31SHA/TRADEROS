"""Phase 9 experiment persistence remains immutable and retry-safe."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from traderos.data.bars import MarketBar
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.timeframes import Timeframe
from traderos.research.models import (
    DatasetManifest,
    ExperimentResult,
    ExperimentSpec,
    ResearchDecision,
    ResearchError,
    ResearchScope,
    TemporalRange,
)
from traderos.research.provenance_cli import main as provenance_cli_main
from traderos.research.registry import ExperimentRegistry, RegisteredExperiment

BASE = datetime(2024, 1, 2, tzinfo=UTC)


def _spec() -> ExperimentSpec:
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
            timestamp=BASE + timedelta(hours=index),
            open=Decimal("1.1"),
            high=Decimal("1.2"),
            low=Decimal("1.0"),
            close=Decimal("1.1"),
            source="fixture",
            ingestion_timestamp=BASE,
        )
        for index in range(2)
    ]
    manifest = DatasetManifest.from_bars(
        dataset_version="registry-fixture-v1",
        bars=bars,
        quality_status="fixture",
        historical_membership_available=True,
        delistings_available=True,
        corporate_actions_verified=True,
    )
    return ExperimentSpec.from_parameters(
        dataset=manifest,
        split_id="registry-split",
        scope=ResearchScope.DEVELOPMENT,
        period=TemporalRange(BASE, BASE + timedelta(hours=2)),
        strategy_id="fixture",
        strategy_version="1",
        parameters={"lookback": 2},
        feature_versions=(),
        regime_configuration_id=None,
        fusion_configuration_id=None,
        risk_policy_configuration_id=None,
        backtest_experiment_id="phase3-fixture",
        cost_scenario_id="base",
        code_version="test",
        random_seed=1,
        selection_eligible=True,
    )


def _registered(*, metric: str = "1") -> RegisteredExperiment:
    spec = _spec()
    return RegisteredExperiment(
        spec=spec,
        result=ExperimentResult(
            experiment_id=spec.experiment_id,
            metrics=(("net_return", metric),),
            decision=ResearchDecision.CONDITIONAL,
            locked_oos_touched=False,
            selection_influenced_by_locked_oos=False,
        ),
    )


def test_registry_persists_exact_lineage_and_identical_retry_is_idempotent(tmp_path: Path) -> None:
    registry = ExperimentRegistry(tmp_path / "registry")
    record = _registered()

    first = registry.record(record)
    second = registry.record(record)
    payload = registry.read(record.spec.experiment_id)

    assert first == second
    assert first.name == f"{record.spec.experiment_id}.json"
    assert payload["result"]["result_hash"] == record.result.result_hash
    assert payload["experiment"]["dataset_hash"] == record.spec.dataset.dataset_hash


def test_registry_lists_a_bounded_deterministic_experiment_index(tmp_path: Path) -> None:
    registry = ExperimentRegistry(tmp_path / "registry")
    first = _registered()
    second_spec = replace(first.spec, parameters=(("lookback", "3"),))
    second = RegisteredExperiment(
        spec=second_spec,
        result=replace(first.result, experiment_id=second_spec.experiment_id),
    )
    registry.record(second)
    registry.record(first)
    (registry.root / "unrelated.json").write_text("{}", encoding="utf-8")
    (registry.root / "research-not-a-file.json").mkdir()

    expected = tuple(sorted((first.spec.experiment_id, second.spec.experiment_id)))
    assert registry.list_experiment_ids() == expected
    assert registry.list_experiment_ids(limit=1) == expected[:1]
    with pytest.raises(ResearchError, match="limit must be positive"):
        registry.list_experiment_ids(limit=0)
    assert ExperimentRegistry(tmp_path / "missing").list_experiment_ids() == ()


def test_provenance_cli_lists_registry_ids_without_database_access(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry = ExperimentRegistry(tmp_path / "registry")
    record = _registered()
    registry.record(record)
    monkeypatch.setenv("TRADEROS_EXPERIMENT_REGISTRY", str(registry.root))
    monkeypatch.delenv("TRADEROS_DATABASE_URL", raising=False)

    assert provenance_cli_main(["--list-experiments", "--experiment-limit", "1"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "experiment_limit": 1,
        "experiments": [record.spec.experiment_id],
    }


def test_registry_rejects_conflicting_retry_and_unsafe_or_missing_reads(tmp_path: Path) -> None:
    registry = ExperimentRegistry(tmp_path / "registry")
    registry.record(_registered())

    with pytest.raises(ResearchError, match="conflicting"):
        registry.record(_registered(metric="2"))
    with pytest.raises(ResearchError, match="unsafe"):
        registry.read("../not-an-experiment")
    with pytest.raises(ResearchError, match="does not exist"):
        registry.read("research-does-not-exist")


def test_registry_rejects_bad_root_and_corrupt_or_unknown_documents(tmp_path: Path) -> None:
    root_file = tmp_path / "not-a-directory"
    root_file.write_text("fixture", encoding="utf-8")
    with pytest.raises(ResearchError, match="unable"):
        ExperimentRegistry(root_file).record(_registered())

    registry = ExperimentRegistry(tmp_path / "registry")
    registry.root.mkdir()
    corrupt_path = registry.root / "research-corrupt.json"
    corrupt_path.write_text("{", encoding="utf-8")
    with pytest.raises(ResearchError, match="valid JSON"):
        registry.read("research-corrupt")
    unknown_path = registry.root / "research-unknown.json"
    unknown_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ResearchError, match="unknown schema"):
        registry.read("research-unknown")


def test_registry_rejects_tampered_result_hash_and_records_locked_oos_access(
    tmp_path: Path,
) -> None:
    registry = ExperimentRegistry(tmp_path / "registry")
    record = _registered()
    path = registry.record(record)
    original_bytes = path.read_bytes()
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["result"]["result_hash"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ResearchError, match="hash mismatch"):
        registry.read(record.spec.experiment_id)
    path.write_bytes(original_bytes)

    oos = replace(
        record.spec,
        scope=ResearchScope.LOCKED_OUT_OF_SAMPLE,
        selection_eligible=False,
        frozen_from_experiment_id=record.spec.experiment_id,
    )
    access = registry.record_oos_access(experiment=oos, accessed_at=BASE)
    assert access.exists()
    assert registry.record_oos_access(experiment=oos, accessed_at=BASE) == access


def test_registry_rejects_unregistered_or_incompatible_oos_parent(tmp_path: Path) -> None:
    registry = ExperimentRegistry(tmp_path / "registry")
    record = _registered()
    oos = replace(
        record.spec,
        scope=ResearchScope.LOCKED_OUT_OF_SAMPLE,
        selection_eligible=False,
        frozen_from_experiment_id="research-missing-parent",
    )
    with pytest.raises(ResearchError, match="not registered"):
        registry.record_oos_access(experiment=oos, accessed_at=BASE)

    registry.record(record)
    incompatible = replace(
        oos,
        frozen_from_experiment_id=record.spec.experiment_id,
        strategy_version="2",
    )
    with pytest.raises(ResearchError, match="lineage differs"):
        registry.record_oos_access(experiment=incompatible, accessed_at=BASE)


def test_locked_oos_result_flags_must_match_scope_and_contamination_is_invalid() -> None:
    spec = _spec()
    with pytest.raises(ResearchError, match="locked-OOS flag"):
        RegisteredExperiment(
            spec=spec,
            result=ExperimentResult(
                experiment_id=spec.experiment_id,
                metrics=(),
                decision=ResearchDecision.INVALID,
                locked_oos_touched=True,
                selection_influenced_by_locked_oos=False,
            ),
        )
    with pytest.raises(ResearchError, match="must be classified invalid"):
        ExperimentResult(
            experiment_id=spec.experiment_id,
            metrics=(),
            decision=ResearchDecision.PASS,
            locked_oos_touched=True,
            selection_influenced_by_locked_oos=True,
        )
    with pytest.raises(ResearchError, match="identity"):
        ExperimentResult(
            experiment_id=" ",
            metrics=(),
            decision=ResearchDecision.INVALID,
            locked_oos_touched=False,
            selection_influenced_by_locked_oos=False,
        )
    with pytest.raises(ResearchError, match="metric names"):
        ExperimentResult(
            experiment_id=spec.experiment_id,
            metrics=(("metric", "1"), ("metric", "2")),
            decision=ResearchDecision.INVALID,
            locked_oos_touched=False,
            selection_influenced_by_locked_oos=False,
        )
    with pytest.raises(ResearchError, match="only a touched"):
        ExperimentResult(
            experiment_id=spec.experiment_id,
            metrics=(),
            decision=ResearchDecision.INVALID,
            locked_oos_touched=False,
            selection_influenced_by_locked_oos=True,
        )
    clean = ExperimentResult(
        experiment_id=spec.experiment_id,
        metrics=(),
        decision=ResearchDecision.INVALID,
        locked_oos_touched=False,
        selection_influenced_by_locked_oos=False,
    )
    assert clean.contaminated is False
    assert len(clean.result_hash) == 64
    with pytest.raises(ResearchError, match="result identity"):
        RegisteredExperiment(
            spec=spec,
            result=ExperimentResult(
                experiment_id="research-other",
                metrics=(),
                decision=ResearchDecision.INVALID,
                locked_oos_touched=False,
                selection_influenced_by_locked_oos=False,
            ),
        )
