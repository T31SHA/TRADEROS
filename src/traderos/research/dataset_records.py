"""Read adapters and immutable qualification records for admitted datasets.

The empirical admission module owns raw bytes, normalized JSONL, manifests,
and quality reports.  This module verifies those existing files without
rewriting them.  Research qualification is a separate immutable record
because admission does not persist a research-policy verdict.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import cast
from uuid import uuid4

from traderos.data.time import require_utc
from traderos.research.dataset import DatasetQualificationStatus, ResearchDatasetEligibility
from traderos.research.models import ExperimentInputBinding, ResearchError, TemporalRange


class DatasetRecordStatus(StrEnum):
    VERIFIED = "verified"
    MISSING = "missing"
    INVALID_SCHEMA = "invalid_schema"
    HASH_MISMATCH = "hash_mismatch"
    UNSUPPORTED = "unsupported"


class DatasetClassification(StrEnum):
    EMPIRICAL = "empirical"
    TEST_FIXTURE = "test_fixture"
    UNKNOWN = "unknown"


def _canonical_bytes(payload: object) -> bytes:
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ResearchError("dataset record is not canonically serializable") from exc


def _hash_payload(payload: object) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_json(path: Path) -> object:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON value: {value}")

    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)


def _parse_utc(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} is not text")
    parsed = datetime.fromisoformat(value)
    require_utc(parsed)
    return parsed.astimezone(UTC)


def _safe_identity(value: str, field: str) -> None:
    if not value or value in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise ResearchError(f"unsafe {field}")


def _safe_path(root: Path, *parts: str) -> Path:
    resolved_root = root.expanduser().resolve()
    candidate = (resolved_root.joinpath(*parts)).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ResearchError("dataset artifact path escapes its configured root") from exc
    return candidate


@dataclass(frozen=True)
class VerifiedDatasetRecord:
    """Normalized identity extracted from an immutable admission manifest."""

    dataset_id: str
    manifest_hash: str
    content_hash: str
    normalized_content_hash: str
    schema_version: str
    source: str
    source_version: str | None
    instruments: tuple[str, ...]
    timeframe: str
    coverage: TemporalRange | None
    timestamp_semantics: str
    adjustment_policy: str | None
    quote_semantics: str | None
    normalization_identity: tuple[tuple[str, str], ...]
    calendar_id: str
    raw_artifact_hashes: tuple[str, ...]
    quality_report_hash: str
    classification: DatasetClassification
    manifest_payload: Mapping[str, object]

    def __post_init__(self) -> None:
        _safe_identity(self.dataset_id, "dataset identity")
        for value in (
            self.manifest_hash,
            self.content_hash,
            self.normalized_content_hash,
            self.schema_version,
            self.source,
            self.timeframe,
            self.timestamp_semantics,
            self.calendar_id,
            self.quality_report_hash,
        ):
            if not value.strip():
                raise ResearchError("dataset record identity fields must not be blank")
        if not self.instruments or any(not value.strip() for value in self.instruments):
            raise ResearchError("dataset record requires instruments")
        if not self.raw_artifact_hashes:
            raise ResearchError("dataset record requires raw artifact references")


@dataclass(frozen=True)
class DatasetRecordRead:
    dataset_id: str
    status: DatasetRecordStatus
    record: VerifiedDatasetRecord | None
    manifest_status: DatasetRecordStatus
    normalized_status: DatasetRecordStatus
    raw_status: DatasetRecordStatus
    quality_report_status: DatasetRecordStatus
    failure_code: str | None = None
    normalized_path: Path | None = None


@dataclass(frozen=True)
class SliceVerification:
    status: DatasetRecordStatus
    code: str | None
    evaluation_rows: int
    warmup_rows: int
    slice_content_hash: str | None


@dataclass(frozen=True)
class DatasetQualificationRecord:
    """Immutable research qualification separate from market-content identity."""

    dataset_id: str
    dataset_content_hash: str
    manifest_hash: str
    policy_id: str
    policy_version: str
    implementation_id: str
    implementation_version: str
    verdict: DatasetQualificationStatus
    empirical_eligibility: ResearchDatasetEligibility
    checked_at: datetime
    check_results: tuple[tuple[str, str], ...]
    warnings: tuple[str, ...]
    unavailable_checks: tuple[str, ...]
    report_reference: str | None
    fixture: bool

    def __post_init__(self) -> None:
        for value in (
            self.dataset_id,
            self.dataset_content_hash,
            self.manifest_hash,
            self.policy_id,
            self.policy_version,
            self.implementation_id,
            self.implementation_version,
        ):
            if not value.strip():
                raise ResearchError("qualification identity fields must not be blank")
        require_utc(self.checked_at)
        if any(not name.strip() or not value.strip() for name, value in self.check_results):
            raise ResearchError("qualification check results must be named and non-blank")
        if any(not value.strip() for value in (*self.warnings, *self.unavailable_checks)):
            raise ResearchError("qualification warnings must be non-blank")
        if (
            self.empirical_eligibility is ResearchDatasetEligibility.EMPIRICALLY_QUALIFIED_DATASET
            and (self.verdict is not DatasetQualificationStatus.QUALIFIED or self.fixture)
        ):
            raise ResearchError("empirical eligibility requires non-fixture qualification")
        if (
            self.fixture
            and self.empirical_eligibility
            is not ResearchDatasetEligibility.DETERMINISTIC_TEST_FIXTURE
        ):
            raise ResearchError("fixture qualification requires fixture eligibility")

    @property
    def qualification_id(self) -> str:
        return _hash_payload(self.canonical_payload())

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": "dataset-qualification.v1",
            "dataset_id": self.dataset_id,
            "dataset_content_hash": self.dataset_content_hash,
            "manifest_hash": self.manifest_hash,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "implementation_id": self.implementation_id,
            "implementation_version": self.implementation_version,
            "verdict": self.verdict.value,
            "empirical_eligibility": self.empirical_eligibility.value,
            "checked_at": self.checked_at.isoformat(),
            "check_results": [list(item) for item in self.check_results],
            "warnings": list(self.warnings),
            "unavailable_checks": list(self.unavailable_checks),
            "report_reference": self.report_reference,
            "fixture": self.fixture,
        }

    def canonical(self) -> dict[str, object]:
        return {**self.canonical_payload(), "qualification_id": self.qualification_id}

    @classmethod
    def from_canonical(cls, payload: object) -> DatasetQualificationRecord:
        fields = {
            "schema_version",
            "dataset_id",
            "dataset_content_hash",
            "manifest_hash",
            "policy_id",
            "policy_version",
            "implementation_id",
            "implementation_version",
            "verdict",
            "empirical_eligibility",
            "checked_at",
            "check_results",
            "warnings",
            "unavailable_checks",
            "report_reference",
            "fixture",
            "qualification_id",
        }
        if not isinstance(payload, dict) or set(payload) != fields:
            raise ResearchError("qualification record has unknown or missing fields")
        if payload["schema_version"] != "dataset-qualification.v1":
            raise ResearchError("qualification record schema is unsupported")
        identity_fields = (
            "dataset_id",
            "dataset_content_hash",
            "manifest_hash",
            "policy_id",
            "policy_version",
            "implementation_id",
            "implementation_version",
        )
        if any(
            not isinstance(payload[field], str) or not payload[field].strip()
            for field in identity_fields
        ):
            raise ResearchError("qualification identity fields are malformed")
        check_results = payload["check_results"]
        warnings = payload["warnings"]
        unavailable = payload["unavailable_checks"]
        if (
            not isinstance(check_results, list)
            or any(
                not isinstance(item, list)
                or len(item) != 2
                or not isinstance(item[0], str)
                or not isinstance(item[1], str)
                for item in check_results
            )
            or not isinstance(warnings, list)
            or any(not isinstance(item, str) for item in warnings)
            or not isinstance(unavailable, list)
            or any(not isinstance(item, str) for item in unavailable)
        ):
            raise ResearchError("qualification check results are malformed")
        if type(payload["fixture"]) is not bool:
            raise ResearchError("qualification fixture classification is malformed")
        if payload["report_reference"] is not None and not isinstance(
            payload["report_reference"], str
        ):
            raise ResearchError("qualification report reference is malformed")
        try:
            record = cls(
                dataset_id=str(payload["dataset_id"]),
                dataset_content_hash=str(payload["dataset_content_hash"]),
                manifest_hash=str(payload["manifest_hash"]),
                policy_id=str(payload["policy_id"]),
                policy_version=str(payload["policy_version"]),
                implementation_id=str(payload["implementation_id"]),
                implementation_version=str(payload["implementation_version"]),
                verdict=DatasetQualificationStatus(str(payload["verdict"])),
                empirical_eligibility=ResearchDatasetEligibility(
                    str(payload["empirical_eligibility"])
                ),
                checked_at=_parse_utc(payload["checked_at"], "checked_at"),
                check_results=tuple((item[0], item[1]) for item in check_results),
                warnings=tuple(warnings),
                unavailable_checks=tuple(unavailable),
                report_reference=(
                    None
                    if payload["report_reference"] is None
                    else str(payload["report_reference"])
                ),
                fixture=payload["fixture"],
            )
        except (TypeError, ValueError) as exc:
            raise ResearchError("qualification record is malformed") from exc
        if payload["qualification_id"] != record.qualification_id:
            raise ResearchError("qualification record hash mismatch")
        return record


@dataclass(frozen=True)
class QualificationRead:
    qualification_id: str
    status: DatasetRecordStatus
    record: DatasetQualificationRecord | None
    failure_code: str | None = None


class DatasetQualificationRegistry:
    """Small create-only registry for qualification facts absent from admission."""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def record(self, qualification: DatasetQualificationRecord) -> Path:
        _safe_identity(qualification.qualification_id, "qualification identity")
        destination = _safe_path(self._root, f"{qualification.qualification_id}.json")
        encoded = (
            json.dumps(qualification.canonical(), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if destination.read_bytes() != encoded:
                    raise ResearchError("conflicting qualification identity") from None
        except OSError as exc:
            raise ResearchError("unable to persist qualification record") from exc
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
        return destination

    def read(self, qualification_id: str) -> QualificationRead:
        try:
            _safe_identity(qualification_id, "qualification identity")
            path = _safe_path(self._root, f"{qualification_id}.json")
        except ResearchError:
            return QualificationRead(
                qualification_id,
                DatasetRecordStatus.INVALID_SCHEMA,
                None,
                "QUALIFICATION_ID_INVALID",
            )
        try:
            payload = _parse_json(path)
        except FileNotFoundError:
            return QualificationRead(
                qualification_id, DatasetRecordStatus.MISSING, None, "QUALIFICATION_MISSING"
            )
        except (OSError, json.JSONDecodeError, ValueError):
            return QualificationRead(
                qualification_id,
                DatasetRecordStatus.INVALID_SCHEMA,
                None,
                "QUALIFICATION_SCHEMA_INVALID",
            )
        try:
            record = DatasetQualificationRecord.from_canonical(payload)
        except ResearchError as exc:
            status = (
                DatasetRecordStatus.HASH_MISMATCH
                if "hash mismatch" in str(exc)
                else DatasetRecordStatus.INVALID_SCHEMA
            )
            return QualificationRead(qualification_id, status, None, "QUALIFICATION_INVALID")
        if record.qualification_id != qualification_id:
            return QualificationRead(
                qualification_id,
                DatasetRecordStatus.HASH_MISMATCH,
                None,
                "QUALIFICATION_ID_MISMATCH",
            )
        return QualificationRead(qualification_id, DatasetRecordStatus.VERIFIED, record)


class DatasetRecordReader:
    """Verify existing admission manifest, normalized, report, and raw files."""

    _SCHEMAS = frozenset(
        {
            "empirical-forex-jsonl-v2",
            "empirical-forex-ohlcv-jsonl-v1",
            "empirical-forex-tick-m15-jsonl-v1",
        }
    )

    def __init__(
        self,
        manifest_root: Path,
        normalized_root: Path,
        *,
        raw_root: Path | None = None,
        quality_report_root: Path | None = None,
    ) -> None:
        self._manifest_root = manifest_root
        self._normalized_root = normalized_root
        self._raw_root = raw_root
        self._quality_report_root = quality_report_root or manifest_root

    def read(self, dataset_id: str) -> DatasetRecordRead:
        try:
            _safe_identity(dataset_id, "dataset identity")
            manifest_path = _safe_path(self._manifest_root, f"{dataset_id}.json")
        except ResearchError:
            return DatasetRecordRead(
                dataset_id,
                DatasetRecordStatus.INVALID_SCHEMA,
                None,
                DatasetRecordStatus.INVALID_SCHEMA,
                DatasetRecordStatus.UNSUPPORTED,
                DatasetRecordStatus.UNSUPPORTED,
                DatasetRecordStatus.UNSUPPORTED,
                "DATASET_ID_INVALID",
            )
        try:
            payload = _parse_json(manifest_path)
        except FileNotFoundError:
            return self._missing(dataset_id, "DATASET_MANIFEST_MISSING")
        except (OSError, json.JSONDecodeError, ValueError):
            return self._invalid(dataset_id, "DATASET_MANIFEST_SCHEMA_INVALID")
        try:
            record = self._parse_manifest(dataset_id, payload)
        except ResearchError as exc:
            status = (
                DatasetRecordStatus.UNSUPPORTED
                if "unsupported" in str(exc)
                else DatasetRecordStatus.INVALID_SCHEMA
            )
            return DatasetRecordRead(
                dataset_id,
                status,
                None,
                status,
                DatasetRecordStatus.UNSUPPORTED,
                DatasetRecordStatus.UNSUPPORTED,
                DatasetRecordStatus.UNSUPPORTED,
                "DATASET_MANIFEST_UNSUPPORTED"
                if status is DatasetRecordStatus.UNSUPPORTED
                else "DATASET_MANIFEST_SCHEMA_INVALID",
            )
        normalized_status, normalized_path = self._verify_normalized(
            dataset_id, record.normalized_content_hash
        )
        quality_status = self._verify_quality_report(dataset_id, record.quality_report_hash)
        raw_status = self._verify_raw(record)
        overall = (
            DatasetRecordStatus.HASH_MISMATCH
            if DatasetRecordStatus.HASH_MISMATCH in (normalized_status, quality_status, raw_status)
            else DatasetRecordStatus.INVALID_SCHEMA
            if DatasetRecordStatus.INVALID_SCHEMA in (normalized_status, quality_status, raw_status)
            else DatasetRecordStatus.MISSING
            if DatasetRecordStatus.MISSING in (normalized_status, quality_status, raw_status)
            else DatasetRecordStatus.UNSUPPORTED
            if DatasetRecordStatus.UNSUPPORTED in (normalized_status, quality_status, raw_status)
            else DatasetRecordStatus.VERIFIED
        )
        return DatasetRecordRead(
            dataset_id,
            overall,
            record,
            DatasetRecordStatus.VERIFIED,
            normalized_status,
            raw_status,
            quality_status,
            None,
            normalized_path,
        )

    def verify_slice(
        self, read: DatasetRecordRead, binding: ExperimentInputBinding
    ) -> SliceVerification:
        if read.record is None:
            return SliceVerification(read.status, "DATASET_RECORD_UNAVAILABLE", 0, 0, None)
        if (
            read.record.dataset_id != binding.dataset_id
            or read.record.content_hash != binding.dataset_content_hash
            or read.record.manifest_hash != binding.manifest_hash
        ):
            return SliceVerification(
                DatasetRecordStatus.HASH_MISMATCH, "DATASET_BINDING_MISMATCH", 0, 0, None
            )
        if (
            read.normalized_status is not DatasetRecordStatus.VERIFIED
            or read.normalized_path is None
        ):
            return SliceVerification(
                read.normalized_status, "NORMALIZED_DATASET_UNVERIFIED", 0, 0, None
            )
        if binding.data_slice_identity != binding.derived_slice_identity():
            return SliceVerification(
                DatasetRecordStatus.HASH_MISMATCH, "DATA_SLICE_IDENTITY_MISMATCH", 0, 0, None
            )
        if not set(binding.instrument_scope).issubset(read.record.instruments):
            return SliceVerification(
                DatasetRecordStatus.HASH_MISMATCH, "INSTRUMENT_SCOPE_MISMATCH", 0, 0, None
            )
        selected: list[bytes] = []
        evaluation_rows = 0
        warmup_rows = 0
        try:
            with read.normalized_path.open("rb") as handle:
                for raw_line in handle:
                    payload = json.loads(
                        raw_line,
                        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
                    )
                    if not isinstance(payload, dict):
                        raise ValueError("normalized row is not an object")
                    timestamp = _parse_utc(payload.get("timestamp"), "normalized timestamp")
                    if binding.evaluation_period.contains(timestamp):
                        evaluation_rows += 1
                        selected.append(raw_line)
                    elif binding.warmup_period is not None and binding.warmup_period.contains(
                        timestamp
                    ):
                        warmup_rows += 1
                        selected.append(raw_line)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return SliceVerification(
                DatasetRecordStatus.INVALID_SCHEMA, "NORMALIZED_DATASET_SCHEMA_INVALID", 0, 0, None
            )
        if evaluation_rows == 0:
            return SliceVerification(
                DatasetRecordStatus.MISSING, "EVALUATION_SLICE_MISSING", 0, warmup_rows, None
            )
        return SliceVerification(
            DatasetRecordStatus.VERIFIED,
            None,
            evaluation_rows,
            warmup_rows,
            hashlib.sha256(b"".join(selected)).hexdigest(),
        )

    def _parse_manifest(self, dataset_id: str, payload: object) -> VerifiedDatasetRecord:
        if not isinstance(payload, dict):
            raise ResearchError("dataset manifest is not an object")
        schema = payload.get("canonical_schema_version")
        if not isinstance(schema, str) or schema not in self._SCHEMAS:
            raise ResearchError("dataset manifest schema is unsupported")
        if payload.get("dataset_id") != dataset_id:
            raise ResearchError("dataset manifest identity does not match its path")
        required = (
            "source",
            "instrument",
            "timeframe",
            "timestamp_semantics",
            "calendar_id",
            "quality_report_hash",
            "normalized_content_hash",
            "created_at",
        )
        if any(field not in payload for field in required):
            raise ResearchError("dataset manifest is missing required fields")
        if not all(
            isinstance(payload[field], str) and payload[field].strip() for field in required[:-1]
        ):
            raise ResearchError("dataset manifest text fields are malformed")
        raw_hashes = payload.get("source_artifact_hashes", payload.get("raw_artifact_hashes"))
        metadata = payload.get("artifact_metadata", payload.get("raw_artifact_metadata"))
        if not isinstance(raw_hashes, list) or not all(
            isinstance(item, str) and item for item in raw_hashes
        ):
            raise ResearchError("dataset manifest raw artifact references are malformed")
        if (
            not isinstance(metadata, list)
            or not metadata
            or not all(isinstance(item, dict) for item in metadata)
        ):
            raise ResearchError("dataset manifest artifact metadata is malformed")
        metadata_hashes = {
            item.get("sha256") for item in metadata if isinstance(item.get("sha256"), str)
        }
        if metadata_hashes != set(raw_hashes):
            raise ResearchError("dataset manifest artifact references are inconsistent")
        created_at = _parse_utc(payload["created_at"], "created_at")
        del created_at
        coverage_start = payload.get("coverage_start")
        coverage_end = payload.get("coverage_end")
        coverage = None
        if coverage_start is not None or coverage_end is not None:
            coverage = TemporalRange(
                _parse_utc(coverage_start, "coverage_start"),
                _parse_utc(coverage_end, "coverage_end"),
            )
        source_versions = {
            item.get("source_version")
            for item in metadata
            if isinstance(item.get("source_version"), str)
        }
        source_version = next(iter(source_versions)) if len(source_versions) == 1 else None
        normalization = tuple(
            (name, str(payload[name]))
            for name in ("ingestion_version", "canonical_schema_version", "source_mode")
            if name in payload and isinstance(payload[name], str)
        )
        fixture_value = payload.get("deterministic_test_fixture")
        classification = (
            DatasetClassification.TEST_FIXTURE
            if fixture_value is True or str(payload.get("source_mode", "")).lower() == "fixture"
            else DatasetClassification.UNKNOWN
        )
        return VerifiedDatasetRecord(
            dataset_id=dataset_id,
            manifest_hash=_hash_payload(payload),
            content_hash=(
                str(payload["dataset_hash"])
                if isinstance(payload.get("dataset_hash"), str)
                else str(payload["normalized_content_hash"])
            ),
            normalized_content_hash=str(payload["normalized_content_hash"]),
            schema_version=schema,
            source=str(payload["source"]),
            source_version=source_version,
            instruments=(str(payload["instrument"]),),
            timeframe=str(payload["timeframe"]),
            coverage=coverage,
            timestamp_semantics=str(payload["timestamp_semantics"]),
            adjustment_policy=(
                str(payload["adjustment_policy"]) if "adjustment_policy" in payload else None
            ),
            quote_semantics=(
                str(payload.get("quote_configuration"))
                if payload.get("quote_configuration") is not None
                else str(payload["quote_convention"])
                if payload.get("quote_convention") is not None
                else None
            ),
            normalization_identity=normalization,
            calendar_id=str(payload["calendar_id"]),
            raw_artifact_hashes=tuple(raw_hashes),
            quality_report_hash=str(payload["quality_report_hash"]),
            classification=classification,
            manifest_payload=payload,
        )

    def _verify_normalized(
        self, dataset_id: str, expected_hash: str
    ) -> tuple[DatasetRecordStatus, Path | None]:
        path = _safe_path(self._normalized_root, f"{dataset_id}.jsonl")
        if not path.exists():
            return DatasetRecordStatus.MISSING, path
        try:
            actual = _hash_file(path)
        except OSError:
            return DatasetRecordStatus.INVALID_SCHEMA, path
        return (
            DatasetRecordStatus.VERIFIED
            if actual == expected_hash
            else DatasetRecordStatus.HASH_MISMATCH,
            path,
        )

    def _verify_quality_report(self, dataset_id: str, expected_hash: str) -> DatasetRecordStatus:
        path = _safe_path(self._quality_report_root, f"{dataset_id}.quality.json")
        if not path.exists():
            return DatasetRecordStatus.MISSING
        try:
            payload = _parse_json(path)
            if not isinstance(payload, dict):
                return DatasetRecordStatus.INVALID_SCHEMA
            return (
                DatasetRecordStatus.VERIFIED
                if _hash_payload(payload) == expected_hash
                else DatasetRecordStatus.HASH_MISMATCH
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError, ResearchError):
            return DatasetRecordStatus.INVALID_SCHEMA

    def _verify_raw(self, record: VerifiedDatasetRecord) -> DatasetRecordStatus:
        if self._raw_root is None:
            return DatasetRecordStatus.UNSUPPORTED
        metadata = record.manifest_payload.get("artifact_metadata")
        if not isinstance(metadata, list):
            return DatasetRecordStatus.INVALID_SCHEMA
        for item in metadata:
            if not isinstance(item, dict):
                return DatasetRecordStatus.INVALID_SCHEMA
            source = item.get("source")
            digest = item.get("sha256")
            filename = item.get("original_filename")
            if not all(isinstance(value, str) and value for value in (source, digest, filename)):
                return DatasetRecordStatus.INVALID_SCHEMA
            source_text = cast(str, source)
            digest_text = cast(str, digest)
            filename_text = cast(str, filename)
            if digest_text not in record.raw_artifact_hashes:
                return DatasetRecordStatus.HASH_MISMATCH
            try:
                _safe_identity(source_text, "raw artifact source")
                _safe_identity(digest_text, "raw artifact identity")
                if Path(filename_text).name != filename_text:
                    return DatasetRecordStatus.INVALID_SCHEMA
                path = _safe_path(self._raw_root, source_text, digest_text, filename_text)
            except ResearchError:
                return DatasetRecordStatus.INVALID_SCHEMA
            if not path.exists():
                return DatasetRecordStatus.MISSING
            try:
                if _hash_file(path) != digest_text:
                    return DatasetRecordStatus.HASH_MISMATCH
            except OSError:
                return DatasetRecordStatus.INVALID_SCHEMA
        return DatasetRecordStatus.VERIFIED

    @staticmethod
    def _missing(dataset_id: str, code: str) -> DatasetRecordRead:
        return DatasetRecordRead(
            dataset_id,
            DatasetRecordStatus.MISSING,
            None,
            DatasetRecordStatus.MISSING,
            DatasetRecordStatus.MISSING,
            DatasetRecordStatus.MISSING,
            DatasetRecordStatus.MISSING,
            code,
        )

    @staticmethod
    def _invalid(dataset_id: str, code: str) -> DatasetRecordRead:
        return DatasetRecordRead(
            dataset_id,
            DatasetRecordStatus.INVALID_SCHEMA,
            None,
            DatasetRecordStatus.INVALID_SCHEMA,
            DatasetRecordStatus.INVALID_SCHEMA,
            DatasetRecordStatus.INVALID_SCHEMA,
            DatasetRecordStatus.INVALID_SCHEMA,
            code,
        )


__all__ = [
    "DatasetClassification",
    "DatasetQualificationRecord",
    "DatasetQualificationRegistry",
    "DatasetRecordReader",
    "DatasetRecordStatus",
    "DatasetRecordRead",
    "QualificationRead",
    "SliceVerification",
    "VerifiedDatasetRecord",
]
