"""Verified dataset and qualification record invariants."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from traderos.research.dataset import DatasetQualificationStatus, ResearchDatasetEligibility
from traderos.research.dataset_records import (
    DatasetQualificationRecord,
    DatasetQualificationRegistry,
    DatasetRecordReader,
    DatasetRecordStatus,
)
from traderos.research.models import ExperimentInputBinding, ResearchError, TemporalRange

BASE = datetime(2024, 1, 2, tzinfo=UTC)


def _write_dataset(
    root: Path, *, dataset_id: str = "dataset-fixture-1"
) -> tuple[DatasetRecordReader, str, str]:
    manifest_root = root / "manifests"
    normalized_root = root / "normalized"
    raw_root = root / "raw"
    manifest_root.mkdir()
    normalized_root.mkdir()
    raw_root.mkdir()
    normalized = b"".join(
        json.dumps(
            {"timestamp": (BASE + timedelta(hours=index)).isoformat(), "instrument": "EUR/USD"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
        for index in range(3)
    )
    content_hash = hashlib.sha256(normalized).hexdigest()
    (normalized_root / f"{dataset_id}.jsonl").write_bytes(normalized)
    raw_bytes = b"synthetic raw fixture"
    raw_hash = hashlib.sha256(raw_bytes).hexdigest()
    (raw_root / "fixture" / raw_hash).mkdir(parents=True)
    (raw_root / "fixture" / raw_hash / "source.csv").write_bytes(raw_bytes)
    quality = {"schema_version": "dataset-quality.v1", "status": "fixture"}
    quality_hash = hashlib.sha256(
        json.dumps(quality, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    (manifest_root / f"{dataset_id}.quality.json").write_text(
        json.dumps(quality, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    manifest = {
        "canonical_schema_version": "empirical-forex-jsonl-v2",
        "dataset_id": dataset_id,
        "source": "fixture",
        "source_artifact_hashes": [raw_hash],
        "instrument": "EUR/USD",
        "timeframe": "1h",
        "timestamp_semantics": "bar_open",
        "calendar_id": "fixture-calendar-v1",
        "quality_report_hash": quality_hash,
        "normalized_content_hash": content_hash,
        "created_at": BASE.isoformat(),
        "coverage_start": BASE.isoformat(),
        "coverage_end": (BASE + timedelta(hours=3)).isoformat(),
        "artifact_metadata": [
            {
                "source": "fixture",
                "source_version": "v1",
                "original_filename": "source.csv",
                "sha256": raw_hash,
            }
        ],
        "ingestion_version": "fixture-ingestion-v1",
        "source_mode": "fixture",
        "quote_convention": "bid_ask",
    }
    (manifest_root / f"{dataset_id}.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    manifest_hash = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return (
        DatasetRecordReader(manifest_root, normalized_root, raw_root=raw_root),
        content_hash,
        manifest_hash,
    )


def _binding(dataset_id: str, content_hash: str, manifest_hash: str) -> ExperimentInputBinding:
    evaluation = TemporalRange(BASE + timedelta(hours=1), BASE + timedelta(hours=3))
    warmup = TemporalRange(BASE, BASE + timedelta(hours=1))
    values = {
        "dataset_id": dataset_id,
        "dataset_content_hash": content_hash,
        "manifest_hash": manifest_hash,
        "evaluation_period": evaluation,
        "warmup_period": warmup,
        "instrument_scope": ("EUR/USD",),
        "feature_input_lineage": ("returns@1",),
    }
    payload = {
        "dataset_id": values["dataset_id"],
        "dataset_content_hash": values["dataset_content_hash"],
        "manifest_hash": values["manifest_hash"],
        "evaluation_period": {
            "start": evaluation.start.isoformat(),
            "end": evaluation.end.isoformat(),
        },
        "warmup_period": {"start": warmup.start.isoformat(), "end": warmup.end.isoformat()},
        "instrument_scope": values["instrument_scope"],
        "feature_input_lineage": values["feature_input_lineage"],
    }
    slice_id = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ExperimentInputBinding(
        **values,
        qualification_id="qualification-fixture-1",
        qualification_policy_id="fixture-policy",
        qualification_policy_version="1",
        data_slice_identity=slice_id,
        fixture=True,
    )


def test_dataset_record_verifies_content_manifest_raw_and_slice(tmp_path: Path) -> None:
    reader, content_hash, manifest_hash = _write_dataset(tmp_path)
    read = reader.read("dataset-fixture-1")
    assert read.status is DatasetRecordStatus.VERIFIED
    assert read.record is not None
    assert read.record.content_hash == content_hash
    assert read.record.manifest_hash == manifest_hash

    slice_read = reader.verify_slice(
        read, _binding("dataset-fixture-1", content_hash, manifest_hash)
    )
    assert slice_read.status is DatasetRecordStatus.VERIFIED
    assert slice_read.warmup_rows == 1
    assert slice_read.evaluation_rows == 2


def test_dataset_tampering_fails_closed(tmp_path: Path) -> None:
    reader, _, _ = _write_dataset(tmp_path)
    path = tmp_path / "normalized" / "dataset-fixture-1.jsonl"
    path.write_bytes(path.read_bytes() + b"tampered")
    read = reader.read("dataset-fixture-1")
    assert read.status is DatasetRecordStatus.HASH_MISMATCH
    assert read.normalized_status is DatasetRecordStatus.HASH_MISMATCH


def test_qualification_is_immutable_and_separates_fixture_from_empirical(tmp_path: Path) -> None:
    qualification = DatasetQualificationRecord(
        dataset_id="dataset-fixture-1",
        dataset_content_hash="c" * 64,
        manifest_hash="m" * 64,
        policy_id="fixture-policy",
        policy_version="1",
        implementation_id="fixture-qualification",
        implementation_version="1",
        verdict=DatasetQualificationStatus.QUALIFIED,
        empirical_eligibility=ResearchDatasetEligibility.DETERMINISTIC_TEST_FIXTURE,
        checked_at=BASE,
        check_results=(("schema", "passed"),),
        warnings=("synthetic fixture",),
        unavailable_checks=("market authenticity",),
        report_reference=None,
        fixture=True,
    )
    registry = DatasetQualificationRegistry(tmp_path / "qualifications")
    first = registry.record(qualification)
    second = registry.record(qualification)
    assert first == second
    assert registry.read(qualification.qualification_id).record == qualification

    payload = json.loads(first.read_text(encoding="utf-8"))
    payload["warnings"] = ["tampered"]
    first.write_text(json.dumps(payload), encoding="utf-8")
    tampered = registry.read(qualification.qualification_id)
    assert tampered.status is DatasetRecordStatus.HASH_MISMATCH


def test_qualification_rejects_fixture_claim_with_empirical_eligibility() -> None:
    with pytest.raises(ResearchError, match="empirical eligibility"):
        DatasetQualificationRecord(
            dataset_id="dataset-fixture-1",
            dataset_content_hash="c" * 64,
            manifest_hash="m" * 64,
            policy_id="policy",
            policy_version="1",
            implementation_id="implementation",
            implementation_version="1",
            verdict=DatasetQualificationStatus.QUALIFIED,
            empirical_eligibility=ResearchDatasetEligibility.EMPIRICALLY_QUALIFIED_DATASET,
            checked_at=BASE,
            check_results=(),
            warnings=(),
            unavailable_checks=(),
            report_reference=None,
            fixture=True,
        )
