"""APEX Milestone 5 preflight and protocol safety invariants."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.research.test_dataset_records import _write_dataset

from traderos.data.empirical import AdmissionPolicy
from traderos.research.apex import (
    ApexProtocol,
    ApexProtocolRegistry,
    SourceIdentity,
    build_blocked_result,
    qualify_admitted_dataset,
)
from traderos.research.dataset_records import DatasetQualificationRegistry
from traderos.research.models import ResearchError

BASE = datetime(2024, 1, 2, tzinfo=UTC)


def _protocol() -> ApexProtocol:
    return ApexProtocol(
        strategy={"strategy_id": "fixture", "strategy_version": "1"},
        hypothesis="A predeclared fixture hypothesis.",
        dataset={"dataset_id": "fixture"},
        temporal_design={
            "development": {
                "start": "2024-01-02T00:00:00+00:00",
                "end": "2024-01-10T00:00:00+00:00",
            },
            "locked_oos": {
                "start": "2024-01-10T00:00:00+00:00",
                "end": "2024-01-20T00:00:00+00:00",
            },
        },
        warmup={
            "period": {"start": "2024-01-01T00:00:00+00:00", "end": "2024-01-02T00:00:00+00:00"},
            "handling": "exclude",
        },
        controls=("always_flat.v1",),
        cost_scenarios=({"id": "base"}, {"id": "adverse"}),
        execution_policy={"id": "next_bar_open"},
        position_sizing_policy={"id": "phase8"},
        risk_configuration={"id": "risk-firewall-v1"},
        metrics=("net_return", "drawdown"),
        sample_adequacy={"minimum_closed_trades": 30},
        statistical_methods={"test_family": "fixture"},
        random_seeds={"bootstrap": 1},
        failure_criteria=("hash mismatch",),
        inconclusive_criteria=("sample too small",),
        oos_access_rules=("record every read",),
        source_identity={"base_commit": "fixture"},
        environment_configuration={"configuration_identity": "fixture"},
    )


def _source() -> SourceIdentity:
    return SourceIdentity(
        base_commit="fixture-commit",
        dirty_state="dirty",
        snapshot_content_digest="s" * 64,
        snapshot_files=(("src/example.py", "f" * 64, 1),),
        python_version="3.14",
        python_executable="python",
        platform_identity="test",
        dependencies=(("traderos", "0.1.0"),),
    )


def _preflight(tmp_path: Path, *, remove_raw: bool = False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    reader, _, _ = _write_dataset(tmp_path)
    if remove_raw:
        raw_file = next((tmp_path / "raw").glob("fixture/*/source.csv"))
        raw_file.unlink()
    preflight = qualify_admitted_dataset(
        reader=reader,
        qualification_registry=DatasetQualificationRegistry(tmp_path / "qualifications"),
        dataset_id="dataset-fixture-1",
        quality_root=tmp_path / "manifests",
        admission_policy=AdmissionPolicy(),
        checked_at=BASE,
    )
    return preflight


def test_protocol_is_deterministic_and_registry_retry_is_idempotent(tmp_path: Path) -> None:
    protocol = _protocol()
    registry = ApexProtocolRegistry(tmp_path / "protocols")
    first = registry.record(protocol)
    second = registry.record(protocol)
    assert first == second
    assert registry.read(protocol.protocol_id)["protocol_hash"] == protocol.protocol_hash
    assert protocol.canonical() == _protocol().canonical()

    payload = json.loads(first.read_text(encoding="utf-8"))
    payload["hypothesis"] = "tampered"
    first.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ResearchError, match="hash mismatch"):
        registry.read(protocol.protocol_id)


def test_missing_raw_artifact_blocks_empirical_qualification(tmp_path: Path) -> None:
    preflight = _preflight(tmp_path, remove_raw=True)
    assert "RAW_ARTIFACT_MISSING" in preflight.blockers
    assert preflight.qualification.fixture is True
    assert preflight.qualification.empirical_eligibility.value == "deterministic_test_fixture"


def test_fixture_is_rejected_even_when_hashes_are_present(tmp_path: Path) -> None:
    preflight = _preflight(tmp_path)
    assert "FIXTURE_DATASET_NOT_ALLOWED" in preflight.blockers
    assert "SOURCE_NOT_PREDECLARED_DUKASCOPY" in preflight.blockers
    assert "TIMEFRAME_SCOPE_MISMATCH" in preflight.blockers
    assert preflight.qualification.fixture is True


def test_protocol_rejects_temporal_overlap_and_warmup_leakage() -> None:
    values = _protocol().__dict__.copy()
    values["temporal_design"] = {
        "development": {"start": "2024-01-02T00:00:00+00:00", "end": "2024-01-11T00:00:00+00:00"},
        "locked_oos": {"start": "2024-01-10T00:00:00+00:00", "end": "2024-01-20T00:00:00+00:00"},
    }
    with pytest.raises(ResearchError, match="overlap"):
        ApexProtocol(**values)

    values = _protocol().__dict__.copy()
    values["warmup"] = {
        "period": {"start": "2024-01-01T00:00:00+00:00", "end": "2024-01-03T00:00:00+00:00"},
        "handling": "exclude",
    }
    with pytest.raises(ResearchError, match="warmup"):
        ApexProtocol(**values)


def test_blocked_result_has_no_strategy_or_trading_side_effects(tmp_path: Path) -> None:
    preflight = _preflight(tmp_path)
    protocol = _protocol()
    result = build_blocked_result(
        protocol=protocol,
        preflight=preflight,
        source_identity=_source(),
        protocol_reference="protocol.json",
        qualification_reference="qualification.json",
        inventory=(),
    )
    assert result["outcome"] == "BLOCKED_DATA"
    assert result["executed"] is False
    assert result["stages"] == {
        "development": {"status": "NOT_EXECUTED", "metrics": "UNAVAILABLE"},
        "walk_forward": {"status": "NOT_EXECUTED", "metrics": "UNAVAILABLE"},
        "locked_oos": {"status": "NOT_EXECUTED", "metrics": "UNAVAILABLE"},
    }
    assert all(value is False for value in result["side_effects"].values())


def test_qualification_registry_retry_does_not_depend_on_wall_clock(tmp_path: Path) -> None:
    first = _preflight(tmp_path / "first")
    second = _preflight(tmp_path / "second")
    assert first.qualification.qualification_id == second.qualification.qualification_id
    assert first.qualification.checked_at == BASE
